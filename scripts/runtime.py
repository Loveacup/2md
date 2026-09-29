"""Shared standard-library runtime helpers for isolated 2md applications."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

STOP_GRACE_SECONDS = 30
STOP_TERM_SECONDS = 5


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def bin_dir(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def venv_exe(venv_dir: Path, name: str) -> Path:
    return bin_dir(venv_dir) / (f"{name}.exe" if os.name == "nt" else name)


def mgmt_env() -> dict:
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def read_pyvenv_cfg(venv_dir: Path) -> dict:
    cfg = {}
    path = venv_dir / "pyvenv.cfg"
    if not path.is_file():
        return cfg
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            cfg[key.strip().lower()] = value.strip()
    return cfg


def isolation_problem(venv_dir: Path, probe: dict):
    prefix = Path(probe["prefix"]).resolve()
    if prefix != venv_dir:
        return f"解释器实际 prefix 为 {prefix}，不是目标 venv {venv_dir}"
    if prefix == Path(probe["base_prefix"]).resolve():
        return f"{prefix} 不是 venv（prefix 等于 base_prefix）"
    if read_pyvenv_cfg(venv_dir).get("include-system-site-packages", "").lower() != "false":
        return "pyvenv.cfg 未声明 include-system-site-packages = false"
    return None


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def wait_group(proc, pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        proc.poll()
        if proc.returncode is not None and not group_alive(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def stop_owned(proc, signum: int) -> int:
    """Stop only this application's process group, never detached shared services."""
    code = 128 + signum
    if os.name == "nt":
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except OSError:
            pass
        try:
            proc.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
        print("⚠️ Windows 下只能确认直接子进程已退出；UI 孙进程是否退出未确认，请手动核对。", file=sys.stderr)
        return code
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGINT)
    except ProcessLookupError:
        pass
    if wait_group(proc, pgid, STOP_GRACE_SECONDS):
        return code
    print(f"→ 应用组 {pgid} {STOP_GRACE_SECONDS}s 内未退出，发送 SIGTERM", file=sys.stderr)
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if wait_group(proc, pgid, STOP_TERM_SECONDS):
        return code
    print(f"→ 应用组 {pgid} 仍未退出，发送 SIGKILL", file=sys.stderr)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if not wait_group(proc, pgid, STOP_TERM_SECONDS):
        print(f"❌ 未能确认应用组 {pgid} 已全部退出", file=sys.stderr)
        return 1
    return code


def run_owned(argv, env) -> int:
    """Run one owned process group and forward cancellation without killing shared daemons."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    received = []

    def on_signal(signum, _frame):
        if not received:
            received.append(signum)

    watched = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        watched.append(signal.SIGHUP)
    if hasattr(signal, "SIGBREAK"):
        watched.append(signal.SIGBREAK)
    previous = {s: signal.signal(s, on_signal) for s in watched}
    try:
        try:
            proc = subprocess.Popen(argv, env=env, **kwargs)
        except OSError as exc:
            print(f"❌ 启动失败: {argv[0]}: {exc}", file=sys.stderr)
            return 1
        while True:
            if received:
                return stop_owned(proc, received[0])
            try:
                rc = proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                continue
            return 128 - rc if rc < 0 else rc
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
