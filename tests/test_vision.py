"""Vision protocol tests use owned fake processes and loopback HTTP only."""
import http.server
import json
import os
from pathlib import Path
import signal
import socketserver
import subprocess
import sys
import threading
import time

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import vision

@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))



def msg(text, model="actual/provider", stop="stop", mid="m1"):
    return {"role": "assistant", "id": mid, "model": model, "stopReason": stop,
            "content": [{"type": "thinking", "thinking": "SECRET THINK"},
                        {"type": "text", "text": text}]}


def events(text, *, final_id="m1", end_id=None, stop="stop", extra=()):
    message = msg(text, mid=final_id, stop=stop)
    ending = dict(message)
    if end_id is not None:
        ending["id"] = end_id
    rows = [{"type": "message_update", "delta": "Working..."},
            *extra, {"type": "message_end", "message": message},
            {"type": "agent_end", "messages": [ending]}]
    return "\n".join(json.dumps(x) for x in rows) + "\n"


def test_omp_uses_completed_final_reply_and_ignores_nontext(tmp_path, monkeypatch):
    script = tmp_path / "omp"
    script.write_text(f"#!{sys.executable}\nimport json,os,sys\n"
                      "if sys.argv[1:3] == ['config','get']:\n"
                      " print(json.dumps({'key':'disabledProviders','value':['provider-x'],'type':'array'})); raise SystemExit()\n"
                      "overlay=sys.argv[sys.argv.index('--config')+1]\n"
                      "open(os.environ['OMP_CAPTURE'],'w').write(json.dumps({'argv':sys.argv,'overlay':json.load(open(overlay))}))\n"
                      "print(" + repr(events('{"transcription":"OCR-73","description":"chart"}')) + ")\n")
    script.chmod(0o700)
    capture = tmp_path / "omp-call.json"
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("OMP_CAPTURE", str(capture))
    image = tmp_path / "x.png"
    image.write_bytes(b"png")
    got = vision.analyze_image(image, backend="omp", model="@vision", base_url=None,
                               api_key_env=None, timeout=5)
    assert got == vision.VisionResult("OCR-73", "chart", "actual/provider")
    call = json.loads(capture.read_text())
    argv, overlay = call["argv"], call["overlay"]
    for flag in ("--no-tools", "--no-lsp", "--no-skills", "--no-rules",
                 "--no-extensions", "--no-title", "--no-prewalk"):
        assert flag in argv
    assert argv[argv.index("--model") + 1] == "@vision"
    assert "@" + str(image.resolve()) in argv
    assert overlay["enabledProviders"] == []
    assert set(("native", "mcp-json", "omp-plugins", "provider-x")) <= set(overlay["disabledProviders"])
    assert overlay["retry"] == {"enabled": False, "modelFallback": False}


@pytest.mark.parametrize("stdout", [
    events('{"transcription":"ok","description":"yes"}', end_id="m2"),
    events('{"transcription":"","description":"ok"}', stop="length"),
    events('{"transcription":"ok","description":"yes"}', extra=({"type":"tool_execution_start"},)),
    events('{"transcription":"ok","description":"yes"}', extra=({"type":"retry_fallback_applied"},)),
    events('{"transcription":"ok","description":"yes"}', stop="aborted"),
])
def test_omp_rejects_invalid_final_protocol(stdout):
    with pytest.raises(RuntimeError):
        vision._omp_events(stdout)
    with pytest.raises(RuntimeError):
        vision._answer('prefix {"transcription":"x","description":"y"}')


def test_answer_allows_empty_transcription_and_single_fence():
    assert vision._answer('```json\n{"transcription":"","description":"visible"}\n```') == ("", "visible")
    with pytest.raises(RuntimeError):
        vision._answer('{"transcription":"x","description":""}')






class Handler(http.server.BaseHTTPRequestHandler):
    seen = []
    redirect = False
    delay = 0
    drip = False
    status = 200
    finish = "stop"
    content = '{"transcription":"OCR-73","description":"picture"}'

    def do_POST(self):
        type(self).seen.append((self.path, self.headers.get("Authorization"), self.headers.get("Proxy-Authorization")))
        if type(self).redirect:
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()
            return
        time.sleep(type(self).delay)
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        body = json.dumps({"model": "served-model", "choices": [{"finish_reason": type(self).finish,
            "message": {"content": type(self).content}}]}).encode()
        if type(self).drip:
            for byte in body:
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
                time.sleep(0.15)
        else:
            self.wfile.write(body)

    def log_message(self, *_):
        pass


def server():
    Handler.seen = []
    Handler.redirect = Handler.drip = False
    Handler.delay = 0
    Handler.status = 200
    Handler.finish = "stop"
    Handler.content = '{"transcription":"OCR-73","description":"picture"}'
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def test_http_backend_disables_redirect_and_secrets_in_diagnostics(tmp_path, monkeypatch, capsys):
    httpd = server()
    try:
        image = tmp_path / "im.png"
        image.write_bytes(b"png")
        Handler.redirect = False
        monkeypatch.setenv("VISION_TEST_KEY", "top-secret-token")
        result = vision.analyze_image(image, backend="openai-compatible", model="local-v",
            base_url=f"http://127.0.0.1:{httpd.server_port}/v1", api_key_env="VISION_TEST_KEY", timeout=4)
        assert result == vision.VisionResult("OCR-73", "picture", "served-model")
        assert Handler.seen == [("/v1/chat/completions", "Bearer top-secret-token", None)]
        Handler.seen.clear()
        Handler.redirect = True
        with pytest.raises(RuntimeError):
            vision.analyze_image(image, backend="openai-compatible", model="local-v",
                base_url=f"http://127.0.0.1:{httpd.server_port}/v1", api_key_env="VISION_TEST_KEY", timeout=4)
        assert [row[0] for row in Handler.seen] == ["/v1/chat/completions"]
        assert "top-secret-token" not in capsys.readouterr().err
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_worker_deadline_stops_only_owned_worker(tmp_path, monkeypatch):
    httpd = server()
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True)
    try:
        Handler.redirect = False
        Handler.delay = 0
        Handler.drip = True
        image = tmp_path / "im.png"
        image.write_bytes(b"png")
        start = time.monotonic()
        with pytest.raises(RuntimeError, match="超时"):
            vision._openai(image, model="local-v", base_url=f"http://127.0.0.1:{httpd.server_port}/v1",
                           api_key_env=None, timeout=0.3)
        assert time.monotonic() - start < 4
        assert sentinel.poll() is None
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=3)
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group ownership assertion")
def test_omp_timeout_stops_owned_child_not_unrelated_process(tmp_path, monkeypatch):
    script = tmp_path / "omp"
    marker = tmp_path / "child-pid"
    script.write_text(f"#!{sys.executable}\nimport json,os,subprocess,sys,time\n"
        "if sys.argv[1:3] == ['config','get']:\n print('{\"value\":[],\"type\":\"array\"}'); raise SystemExit()\n"
        f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); open({str(marker)!r},'w').write(str(p.pid)); time.sleep(30)\n")
    script.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    image = tmp_path / "x.png"
    image.write_bytes(b"x")
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True)
    try:
        with pytest.raises(RuntimeError, match="超时"):
            vision._omp(image, model="@vision", timeout=0.4)
        child_pid = int(marker.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        assert sentinel.poll() is None
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=3)


@pytest.mark.parametrize("status,finish,content", [
    (401, "stop", "top-secret-token"),
    (200, "length", '{"transcription":"partial","description":"partial"}'),
    (200, "stop", '{"transcription":"ok","description":""}'),
    (200, "stop", "I cannot process images"),
    (200, "stop", 'explanation {"transcription":"ok","description":"picture"}'),
])
def test_http_model_errors_fail_without_exposing_payload(tmp_path, status, finish, content):
    httpd=server()
    Handler.status,Handler.finish,Handler.content=status,finish,content
    image=tmp_path/'x.png';image.write_bytes(b'png')
    try:
        with pytest.raises(RuntimeError) as error:
            vision.analyze_image(image,backend='openai-compatible',model='local',
                                 base_url=f'http://127.0.0.1:{httpd.server_port}/v1',api_key_env=None,timeout=3)
        assert 'top-secret-token' not in str(error.value)
        assert len(Handler.seen) == 1
    finally:
        httpd.shutdown();httpd.server_close()


def test_omp_completed_at_decoration_does_not_change_final_identity():
    message=msg('{"transcription":"OCR-73","description":"picture"}')
    message['provider']='fixture'
    decorated=dict(message,completedAt=123)
    stdout='\n'.join(json.dumps(e) for e in [
        {'type':'message_end','message':decorated},
        {'type':'agent_end','messages':[message]}])
    text,model=vision._omp_events(stdout)
    assert vision._answer(text) == ('OCR-73','picture')
    assert model == 'fixture/actual/provider'


@pytest.mark.skipif(os.name == 'nt',reason='POSIX owned process groups')
def test_sigterm_conversion_stops_nested_omp_and_preserves_failed_delivery(tmp_path, monkeypatch):
    from PIL import Image
    fake=tmp_path/'omp';pidfile=tmp_path/'child.pid'
    fake.write_text(f'#!{sys.executable}\nimport json,os,sys,time\n'
                    "if sys.argv[1:3]==['config','get']:\n print(json.dumps({'value':[]}));raise SystemExit()\n"
                    f"open({str(pidfile)!r},'w').write(str(os.getpid()));time.sleep(60)\n")
    fake.chmod(0o700)
    source=tmp_path/'image.png';Image.new('RGB',(30,20),'red').save(source)
    env=dict(os.environ,PATH=str(tmp_path)+os.pathsep+os.environ['PATH'],JZ2MD_VENV=sys.prefix)
    proc=subprocess.Popen([sys.executable,str(SCRIPTS/'2md.py'),'convert',str(source),'--output-dir',str(tmp_path/'out')],
                          env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    sentinel=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
    try:
        deadline=time.monotonic()+15
        while not pidfile.exists() and time.monotonic()<deadline:
            assert proc.poll() is None
            time.sleep(.05)
        assert pidfile.exists()
        child=int(pidfile.read_text())
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=15)
        assert proc.returncode == 143
        with pytest.raises(ProcessLookupError):os.kill(child,0)
        assert sentinel.poll() is None
        destination=tmp_path/'out/image'
        assert json.loads((destination/'conversion.json').read_text())['status']=='failed'
        assert not (destination/'image.md').exists()
    finally:
        if proc.poll() is None:proc.kill();proc.communicate()
        sentinel.terminate();sentinel.wait(timeout=3)


def test_omp_missing_protection_fails_before_image_request(tmp_path, monkeypatch):
    script=tmp_path/'omp';requested=tmp_path/'requested'
    script.write_text(f"#!{sys.executable}\nimport sys\n"
                      "if sys.argv[1:3]==['config','get']:\n"
                      " if sys.argv[3]=='retry.modelFallback':raise SystemExit(2)\n"
                      " print('{\"value\":[]}');raise SystemExit()\n"
                      f"open({str(requested)!r},'w').write('started')\n")
    script.chmod(0o700)
    monkeypatch.setenv('PATH',str(tmp_path)+os.pathsep+os.environ['PATH'])
    with pytest.raises(RuntimeError,match='retry.modelFallback'):
        vision._omp(tmp_path/'image.png',model='@vision',timeout=3)
    assert not requested.exists()
