#!/usr/bin/env python3
"""Isolated launcher for the lightweight 2md conversion environment (Python 3.9+)."""
from __future__ import annotations

import argparse
import json
import os
import re
import math
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from urllib.parse import urlsplit

import runtime

SCRIPT_PATH = Path(__file__).resolve()
CONVERTER = (SCRIPT_PATH.parent / "convert_document.py").resolve()
REQUIREMENTS = (SCRIPT_PATH.parent / "requirements-markitdown.txt").resolve()
VENV_ENV = "JZ2MD_VENV"
MARKER_VENV_ENV = "JZ2MD_MARKER_VENV"
MIN_PY = (3, 10)
MAX_PY = (3, 15)
CORE_DISTS = ("markitdown", "Pillow", "pypdfium2", "markdown-it-py", "CairoSVG")


class UsageError(Exception):
    """Invalid arguments or unsafe environment paths (exit 2)."""


def _version_ok(version) -> bool:
    return MIN_PY <= tuple(version[:2]) < MAX_PY


def _selected_path(env_name: str, default: Path) -> Path:
    raw = os.environ.get(env_name)
    if raw is None:
        return default.resolve()
    if not raw.strip():
        raise UsageError(f"{env_name} 已设置为空值；请给出绝对路径或取消该变量")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise UsageError(f"{env_name} 必须是绝对路径")
    return candidate.resolve()


def resolve_venv() -> Path:
    target = _selected_path(VENV_ENV, Path.home() / ".venvs" / "2md")
    legacy = (Path.home() / ".venvs" / "pdf-skill").resolve()
    marker = _selected_path(MARKER_VENV_ENV, Path.home() / ".venvs" / "2pdf-marker")
    protected = (("旧排版环境", legacy), ("Marker 环境", marker), ("当前全局 Python", Path(sys.base_prefix).resolve()))
    for label, path in protected:
        if runtime.is_within(target, path) or runtime.is_within(path, target):
            raise UsageError(f"拒绝使用 {target}：与{label} {path} 重叠")
    return target


def _probe(venv_dir: Path) -> dict:
    py = runtime.venv_exe(venv_dir, "python")
    code = r"""
import json, sys
from importlib import metadata
names = json.loads(sys.argv[1])
def ver(name):
    try: return metadata.version(name)
    except metadata.PackageNotFoundError: return None
print(json.dumps({"prefix": sys.prefix, "base_prefix": sys.base_prefix,
                  "version": list(sys.version_info[:3]),
                  "dists": {name: ver(name) for name in names}}))
"""
    try:
        proc = subprocess.run([str(py), "-I", "-B", "-c", code, json.dumps(CORE_DISTS)],
                              capture_output=True, text=True, env=runtime.mgmt_env(), timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"无法运行目标解释器 {py}: {exc}") from exc
    if proc.returncode:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["无输出"]
        raise RuntimeError(f"目标解释器探测失败（退出 {proc.returncode}）: {tail[0]}")
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError("目标解释器探测输出无法解析") from exc


def _read_pins() -> dict:
    pins = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9_,.-]+\])?==([^\s;]+)", line)
        if match:
            pins[match.group(1).lower().replace("_", "-")] = match.group(2)
    return pins


def _collect_env_checks(venv_dir: Path) -> list:
    checks = []
    def add(name, status, detail=""):
        checks.append({"name": name, "status": status, "detail": detail})

    py = runtime.venv_exe(venv_dir, "python")
    add("venv:path", "info", str(venv_dir))
    if not py.is_file():
        add("venv:python", "fail", f"未找到 {py}；先运行 python3.12 {SCRIPT_PATH} setup")
        return checks
    add("venv:python", "ok", str(py))
    try:
        probe = _probe(venv_dir)
    except RuntimeError as exc:
        add("venv:probe", "fail", str(exc))
        return checks
    problem = runtime.isolation_problem(venv_dir, probe)
    add("venv:isolation", "fail" if problem else "ok", problem or "独立 prefix，未继承系统 site-packages")
    version = probe["version"]
    add("python:version", "ok" if _version_ok(version) else "fail",
        f"Python {'.'.join(map(str, version))}（要求 >=3.10,<3.15）")
    pins = _read_pins()
    dists = probe["dists"]
    for name in CORE_DISTS:
        want = pins.get(name.lower().replace("_", "-"))
        have = dists.get(name)
        ok = have is not None and (want is None or have == want)
        detail = f"已装 {have}，要求 =={want}" if want and have != want else (have or f"未安装；运行 python3.12 {SCRIPT_PATH} setup")
        add(f"dist:{name}", "ok" if ok else "fail", detail)
    return checks


def _validate_vision(backend, model, base_url, api_key_env, *, required: bool):
    if backend not in (None, "omp", "openai-compatible"):
        raise UsageError("--vision-backend 必须是 omp 或 openai-compatible")
    selected = backend or "omp"
    if model is not None and not model.strip():
        raise UsageError("--vision-model 不能为空")
    if api_key_env is not None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
            raise UsageError("--api-key-env 必须是合法环境变量名")
        if required and not os.environ.get(api_key_env):
            raise UsageError(f"--api-key-env 指定的环境变量 {api_key_env} 不存在或为空")
    if selected == "openai-compatible" and required and (not base_url or not model):
        raise UsageError("openai-compatible 需要显式提供 --base-url 与 --vision-model")
    if base_url is not None:
        try:
            parsed = urlsplit(base_url)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname
                     and parsed.username is None and parsed.password is None
                     and not parsed.query and not parsed.fragment
                     and parsed.path.rstrip("/").endswith("/v1"))
            # urllib validates malformed/out-of-range ports lazily on access.
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise UsageError("--base-url 必须是无凭据、query 或 fragment 的 HTTP(S) /v1 根 URL")


def _preflight_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{SCRIPT_PATH.name} preflight")
    parser.add_argument("--vision-backend", choices=("omp", "openai-compatible"))
    parser.add_argument("--vision-model")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key-env")
    parser.add_argument("--json", action="store_true")
    return parser


def cmd_preflight(args) -> int:
    ns = _preflight_parser().parse_args(args)
    _validate_vision(ns.vision_backend, ns.vision_model, ns.base_url, ns.api_key_env,
                     required=ns.vision_backend == "openai-compatible")
    venv_dir = resolve_venv()
    checks = _collect_env_checks(venv_dir)
    marker_path = _selected_path(MARKER_VENV_ENV, Path.home() / ".venvs" / "2pdf-marker")
    marker_python = runtime.venv_exe(marker_path, "python")
    checks.append({"name": "marker:environment", "status": "info",
                   "detail": f"高级路径解释器: {marker_python}（未请求，未验证）"})
    if ns.vision_backend is None:
        checks.append({"name": "vision:omp", "status": "info",
                       "detail": f"PATH 可见: {shutil.which('omp')}" if shutil.which("omp") else "未检查；显式选择 --vision-backend omp 时要求可执行文件"})
    elif ns.vision_backend == "omp":
        omp = shutil.which("omp")
        checks.append({"name": "vision:omp", "status": "ok" if omp else "fail",
                       "detail": omp or "PATH 中未找到 omp"})
    else:
        checks.append({"name": "vision:openai-compatible", "status": "ok",
                       "detail": "参数有效；预检不发送请求"})
    failed = any(item["status"] == "fail" for item in checks)
    overall = "fail" if failed else "ok"
    if ns.json:
        print(json.dumps({"checks": checks, "overall": overall}, ensure_ascii=False))
    else:
        marks = {"ok": "✅", "fail": "❌", "info": "ℹ️ "}
        for item in checks:
            print(f"  {marks.get(item['status'], '  ')} {item['name']:30} {item['detail']}")
        print(f"  === overall: {overall.upper()} ===")
    return 1 if failed else 0


def cmd_setup(args) -> int:
    if args:
        raise UsageError("setup 不接受参数")
    venv_dir = resolve_venv()
    py = runtime.venv_exe(venv_dir, "python")
    if venv_dir.exists() and not venv_dir.is_dir():
        print(f"❌ {venv_dir} 已存在且不是目录；未修改", file=sys.stderr)
        return 1
    is_empty = not venv_dir.exists() or not any(venv_dir.iterdir())
    if is_empty:
        if not _version_ok(sys.version_info):
            print(f"❌ 新建轻量环境需要 Python >=3.10,<3.15；当前 {sys.executable} 为 {sys.version.split()[0]}。请用 python3.12 启动", file=sys.stderr)
            return 1
        print(f"→ 创建独立 venv: {venv_dir}（{sys.executable}）")
        try:
            venv.EnvBuilder(with_pip=True, system_site_packages=False,
                            symlinks=(os.name != "nt")).create(str(venv_dir))
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"❌ 创建 venv 失败: {exc}", file=sys.stderr)
            return 1
    elif not (venv_dir / "pyvenv.cfg").is_file() or not py.is_file():
        print(f"❌ {venv_dir} 非空且不是 venv；为保护现有内容，未修改。请改用新路径（{VENV_ENV}）", file=sys.stderr)
        return 1
    try:
        probe = _probe(venv_dir)
    except RuntimeError as exc:
        print(f"❌ {exc}；禁止安装", file=sys.stderr)
        return 1
    problem = runtime.isolation_problem(venv_dir, probe)
    if problem:
        print(f"❌ {problem}；禁止安装", file=sys.stderr)
        return 1
    if not _version_ok(probe["version"]):
        print(f"❌ {py} 的 Python 版本不满足 >=3.10,<3.15；该目录未安装", file=sys.stderr)
        return 1
    env = runtime.mgmt_env()
    install = [str(py), "-I", "-m", "pip", "install", "-r", str(REQUIREMENTS)]
    print("→ " + " ".join(install))
    if subprocess.call(install, env=env) != 0:
        print("❌ pip 安装失败", file=sys.stderr)
        return 1
    print("→ pip check")
    if subprocess.call([str(py), "-I", "-m", "pip", "check"], env=env) != 0:
        print("❌ pip check 报告依赖冲突", file=sys.stderr)
        return 1
    print("→ preflight")
    return 1 if any(item["status"] == "fail" for item in _collect_env_checks(venv_dir)) else 0


def _runtime_env(venv_dir: Path) -> dict:
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        env.pop(key, None)
    env["VIRTUAL_ENV"] = str(venv_dir)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PATH"] = str(runtime.bin_dir(venv_dir)) + os.pathsep + env.get("PATH", "")
    return env


def _require_ready(venv_dir: Path) -> bool:
    failures = [item for item in _collect_env_checks(venv_dir) if item["status"] == "fail"]
    if failures:
        print(f"❌ 轻量环境未就绪（{venv_dir}）：", file=sys.stderr)
        for item in failures:
            print(f"   - {item['name']}: {item['detail']}", file=sys.stderr)
        return False
    return True


def cmd_convert(args) -> int:
    parser = argparse.ArgumentParser(prog=f"{SCRIPT_PATH.name} convert")
    parser.add_argument("input")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--engine", choices=("auto", "markitdown", "marker"), default="auto")
    parser.add_argument("--vision", choices=("on", "off"), default="on")
    parser.add_argument("--vision-backend", choices=("omp", "openai-compatible"), default="omp")
    parser.add_argument("--vision-model")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key-env")
    parser.add_argument("--vision-timeout", type=float, default=300)
    parser.add_argument("--page-images", action="store_true")
    ns = parser.parse_args(args)
    if not ns.input.strip() or not ns.output_dir.strip():
        raise UsageError("输入文件与 --output-dir 不能是空路径")
    input_path = Path(ns.input).expanduser()
    if not input_path.exists() or not input_path.is_file():
        raise UsageError(f"输入必须是存在的本地普通文件：{input_path}")
    if not math.isfinite(ns.vision_timeout) or ns.vision_timeout <= 0:
        raise UsageError("--vision-timeout 必须是正数")
    _validate_vision(ns.vision_backend, ns.vision_model, ns.base_url, ns.api_key_env,
                     required=ns.vision == "on" and ns.vision_backend == "openai-compatible")
    if ns.page_images and input_path.suffix.lower() != ".pdf":
        raise UsageError("--page-images 仅支持 PDF 输入")
    venv_dir = resolve_venv()
    if not _require_ready(venv_dir):
        return 1
    command = [str(runtime.venv_exe(venv_dir, "python")), str(CONVERTER), *args]
    return runtime.run_owned(command, _runtime_env(venv_dir))


def cmd_python(args) -> int:
    if not args or args[0] != "--" or len(args) < 2:
        raise UsageError("用法: python -- SCRIPT [ARGS...]")
    script_args = args[1:]
    if script_args[0].startswith("-"):
        raise UsageError("python 操作只接受脚本路径，不接受解释器选项")
    script = Path(script_args[0]).expanduser()
    if not script.is_file():
        raise UsageError(f"脚本不是存在的普通文件：{script}")
    venv_dir = resolve_venv()
    if not _require_ready(venv_dir):
        return 1
    return runtime.run_owned([str(runtime.venv_exe(venv_dir, "python")), *script_args], _runtime_env(venv_dir))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(f"用法: python3 {SCRIPT_PATH} <setup|preflight|convert|python> [参数]", file=sys.stderr)
        return 2
    op, rest = argv[0], argv[1:]
    if op in ("-h", "--help", "help"):
        print(f"用法: python3 {SCRIPT_PATH} <setup|preflight|convert|python> [参数]\n"
              "  setup\n  preflight [--vision-backend omp|openai-compatible] [--vision-model MODEL] [--base-url URL] [--api-key-env NAME] [--json]\n"
              "  convert INPUT --output-dir OUT [--engine auto|markitdown|marker] [--vision on|off] [--vision-backend omp|openai-compatible] [--vision-model MODEL] [--base-url URL] [--api-key-env NAME] [--vision-timeout SECONDS] [--page-images]\n"
              "  python -- SCRIPT [ARGS...]\n"
              f"环境变量: {VENV_ENV}（默认 ~/.venvs/2md）；Marker 独立环境由 {MARKER_VENV_ENV} 管理")
        return 0
    try:
        if op == "setup":
            return cmd_setup(rest)
        if op == "preflight":
            return cmd_preflight(rest)
        if op == "convert":
            return cmd_convert(rest)
        if op == "python":
            return cmd_python(rest)
        raise UsageError(f"未知操作 {op!r}")
    except UsageError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2


if __name__ == "__main__":
    sys.exit(main())
