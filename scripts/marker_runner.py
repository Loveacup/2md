#!/usr/bin/env python3
"""2pdf 结构化解析链（Marker）的独立入口：建立/检查专用 venv，并启动上游原生命令。

与排版引擎 md2pdf_chrome.py 互不 import、互不共享解释器：
- 环境默认 ~/.venvs/2pdf-marker，可用 JZ2MD_MARKER_VENV 指定；拒绝旧 ~/.venvs/pdf-skill 及全局 Python。
- 管理探测与 pip 一律在目标 venv 的 `python -I` 中执行，不受父进程 PYTHONPATH 影响。
- single/batch/gui/server/python 之后的参数原样交给上游，不重解析 Marker 参数。
- 只管理本次启动的应用进程组；Surya 自行 detached 的共享模型服务不归本脚本管。

本文件须兼容 Python 3.9（可用旧默认 python3 看帮助、预检、启动已建好的环境）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import runtime

SCRIPT_PATH = Path(__file__).resolve()
REQUIREMENTS = SCRIPT_PATH.parent / "requirements-marker.txt"
VENV_ENV = "JZ2MD_MARKER_VENV"
EXTRA_PYTHONPATH_ENV = "JZ2MD_MARKER_PYTHONPATH"
MIN_PY = (3, 10)
MAX_PY = (4, 0)

# 上游 pyproject 声明的可选依赖（GUI/server 位于 dev group，不在 [full] 内）。
EXTRA_SPECS = {
    "full": ["marker-pdf[full]"],
    "gui": ["streamlit>=1.37.1", "streamlit-ace>=0.1.1"],
    "server": ["fastapi>=0.115.4", "uvicorn>=0.32.0", "python-multipart>=0.0.16"],
}
EXTRA_DISTS = {
    "gui": ["streamlit", "streamlit-ace"],
    "server": ["fastapi", "uvicorn", "python-multipart"],
}
CORE_ENTRIES = ("marker", "marker_single")
EXTRA_ENTRIES = {"gui": ("marker_gui", "streamlit"), "server": ("marker_server",)}
RUN_ENTRIES = {
    "single": "marker_single",
    "batch": "marker",
    "gui": "marker_gui",
    "server": "marker_server",
    "python": "python",
}
MODEL_SERVICE_URL_ENVS = ("SURYA_INFERENCE_URL", "FAST_LAYOUT_SERVER_URL", "OCR_ERROR_SERVER_URL")
STOP_GRACE_SECONDS = 30
STOP_TERM_SECONDS = 5

USAGE = f"""\
用法: python3 {SCRIPT_PATH.name} <操作> [参数]

管理（在目标 venv 的隔离解释器中执行）：
  setup [--full] [--gui] [--server]      建立/补齐独立 venv（新建需 Python >=3.10,<4，例如 python3.12）
  preflight [--full] [--gui] [--server] [--json]
                                         只读预检；缺核心或所选依赖退出 1

运行（`--` 之后的参数原样交给上游）：
  single -- <marker_single 参数>          单文件解析，例如 single -- in.pdf --output_format json --output_dir out
  batch  -- <marker 参数>                 目录批量解析（上游只扫描第一层）
  gui    -- <marker_gui 参数>             Streamlit 界面（需 setup --gui）
  server -- <marker_server 参数>          本地 HTTP 接口（需 setup --server）
  python -- <python 参数>                 用独立 venv 的 Python 运行脚本（非交互）

环境变量：
  {VENV_ENV}          venv 路径（默认 ~/.venvs/2pdf-marker；空值为参数错误）
  {EXTRA_PYTHONPATH_ENV}    运行时额外导入目录（os.pathsep 分隔的绝对路径；不用于管理探测/pip）

退出码：上游退出码原样返回；环境未就绪/启动失败 1；参数错误 2；Ctrl-C 130。
"""

# 在目标 venv 的 `python -I -B` 中执行：只读 metadata，不 import marker/torch/surya。
_PROBE_CODE = r"""
import json, re, sys
from importlib import metadata

def ver(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None

names = json.loads(sys.argv[1])
out = {
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
    "version": list(sys.version_info[:3]),
    "dists": {n: ver(n) for n in names},
    "full": [],
}
try:
    reqs = metadata.requires("marker-pdf") or []
except metadata.PackageNotFoundError:
    reqs = []
for req in reqs:
    if re.search(r"extra\s*==\s*['\"]full['\"]", req):
        m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", req)
        if m:
            out["full"].append([m.group(1), ver(m.group(1))])
print(json.dumps(out))
"""


class UsageError(Exception):
    """参数或危险路径错误（退出 2）。"""


# ---------------------------------------------------------------------------
# 路径与环境
# ---------------------------------------------------------------------------

def _legacy_venv() -> Path:
    return (Path.home() / ".venvs" / "pdf-skill").resolve()




def resolve_venv() -> Path:
    raw = os.environ.get(VENV_ENV)
    if raw is None:
        target = (Path.home() / ".venvs" / "2pdf-marker").resolve()
    else:
        if not raw.strip():
            raise UsageError(f"{VENV_ENV} 已设置为空值；请给出绝对路径或取消该变量")
        target = Path(raw).expanduser().resolve()
    legacy = _legacy_venv()
    light_raw = os.environ.get("JZ2MD_VENV")
    if light_raw is not None and not light_raw.strip():
        raise UsageError("JZ2MD_VENV 已设置为空值；请给出绝对路径或取消该变量")
    light = (Path(light_raw).expanduser() if light_raw else Path.home() / ".venvs" / "2md").resolve()
    if runtime.is_within(target, legacy) or runtime.is_within(legacy, target):
        raise UsageError(f"拒绝使用 {target}：与排版引擎环境 {legacy} 重叠，Marker 必须使用独立 venv")
    if runtime.is_within(target, light) or runtime.is_within(light, target):
        raise UsageError(f"拒绝使用 {target}：与轻量解析环境 {light} 重叠，Marker 必须使用独立 venv")
    if target == Path(sys.base_prefix).resolve():
        raise UsageError(f"拒绝使用 {target}：这是当前解释器的基础安装目录（全局 Python）")
    return target



def _read_requirements() -> dict:
    pins = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)", line)
        if not m:
            raise RuntimeError(f"{REQUIREMENTS} 只允许 name==version 精确版本行：{line!r}")
        pins[m.group(1)] = m.group(2)
    return pins




def _probe(venv_dir: Path, names) -> dict:
    """在目标解释器中读取 prefix 与包版本；失败时抛 RuntimeError。"""
    py = runtime.venv_exe(venv_dir, "python")
    try:
        proc = subprocess.run(
            [str(py), "-I", "-B", "-c", _PROBE_CODE, json.dumps(list(names))],
            capture_output=True, text=True, env=runtime.mgmt_env(), timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"无法运行 {py}: {exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["无输出"]
        raise RuntimeError(f"{py} 探测失败（退出 {proc.returncode}）: {tail[0]}")
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f"{py} 探测输出无法解析") from exc


def _isolation_problem(venv_dir: Path, probe: dict):
    return runtime.isolation_problem(venv_dir, probe)


def _version_ok(version) -> bool:
    return MIN_PY <= tuple(version[:2]) < MAX_PY


def _setup_hint(features) -> str:
    flags = "".join(f" --{f}" for f in ("full", "gui", "server") if f in features)
    return f"python3.12 {SCRIPT_PATH} setup{flags}"


# ---------------------------------------------------------------------------
# 预检
# ---------------------------------------------------------------------------

def collect_checks(venv_dir: Path, features) -> list:
    checks = []

    def chk(name, status, hint=""):
        checks.append({"name": name, "status": status, "hint": hint})

    hint = _setup_hint(features)
    py = runtime.venv_exe(venv_dir, "python")
    chk("venv:path", "info", str(venv_dir))
    if not py.exists():
        chk("venv:python", "fail", f"未找到 {py}；先运行 {hint}")
    else:
        chk("venv:python", "ok", str(py))
        pins = _read_requirements()
        names = list(pins)
        for feat in ("gui", "server"):
            if feat in features:
                names += EXTRA_DISTS[feat]
        probe = None
        try:
            probe = _probe(venv_dir, names)
        except RuntimeError as exc:
            chk("venv:probe", "fail", str(exc))
        if probe is not None:
            problem = _isolation_problem(venv_dir, probe)
            chk("venv:isolation", "fail" if problem else "ok", problem or "独立 prefix，未继承系统 site-packages")
            ver = ".".join(str(x) for x in probe["version"])
            chk("python:version", "ok" if _version_ok(probe["version"]) else "fail",
                f"Python {ver}（要求 >=3.10,<4）")
            for name, want in pins.items():
                have = probe["dists"].get(name)
                chk(f"dist:{name}", "ok" if have == want else "fail",
                    f"已装 {have}，要求 =={want}" if have != want else have)
            for feat in ("gui", "server"):
                if feat in features:
                    for name in EXTRA_DISTS[feat]:
                        have = probe["dists"].get(name)
                        chk(f"{feat}:{name}", "ok" if have else "fail", have or f"未安装；运行 {hint}")
            if "full" in features:
                if not probe["full"]:
                    chk("full:marker-pdf[full]", "fail", f"无法从 marker-pdf 元数据读取 [full] 依赖；运行 {hint}")
                for name, have in probe["full"]:
                    chk(f"full:{name}", "ok" if have else "fail", have or f"未安装；运行 {hint}")
        entries = list(CORE_ENTRIES)
        for feat in ("gui", "server"):
            if feat in features:
                entries += EXTRA_ENTRIES[feat]
        for entry in entries:
            exe = runtime.venv_exe(venv_dir, entry)
            chk(f"entry:{entry}", "ok" if exe.exists() else "fail",
                str(exe) if exe.exists() else f"venv 内缺 {exe.name}；运行 {hint}")

    # Surya 在非 NVIDIA 机器上自动拉起 llama-server（可用 LLAMA_CPP_BINARY 指定），NVIDIA 上走 docker vLLM。
    llama = shutil.which(os.environ.get("LLAMA_CPP_BINARY") or "llama-server")
    docker = shutil.which("docker")
    chk("path:llama-server", "info", f"PATH 可见: {llama}" if llama else "PATH 不可见")
    chk("path:docker", "info", f"PATH 可见: {docker}" if docker else "PATH 不可见")
    if not (llama or docker or os.environ.get("SURYA_INFERENCE_URL")):
        chk("inference-backend", "warn",
            "无 llama-server/docker 且未配置 SURYA_INFERENCE_URL：OCR、公式、balanced 模式不可用，"
            "仅 --disable_ocr 路径可跑（macOS: brew install llama.cpp）")
    for key in MODEL_SERVICE_URL_ENVS:
        chk(f"env:{key}", "info", "已配置（数据发往该服务，值不回显）" if os.environ.get(key) else "未配置")
    chk("unverified", "info", "模型缓存、模型服务连通性/所有权、WeasyPrint 原生库未由此预检验证；以实际转换 smoke 为准")
    return checks


def _report(checks, as_json: bool) -> int:
    fatal = any(c["status"] == "fail" for c in checks)
    has_warn = any(c["status"] == "warn" for c in checks)
    overall = "fail" if fatal else ("degraded" if has_warn else "ok")
    if as_json:
        print(json.dumps({"checks": checks, "overall": overall}, ensure_ascii=False))
    else:
        marks = {"ok": "✅", "warn": "⚠️ ", "fail": "❌", "info": "ℹ️ "}
        for c in checks:
            print(f"  {marks.get(c['status'], '  ')} {c['name']:30} {c['hint']}")
        print(f"  === overall: {overall.upper()} ===")
    return 1 if fatal else 0


def _feature_parser(prog: str, with_json: bool) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{SCRIPT_PATH.name} {prog}")
    parser.add_argument("--full", action="store_true", help="marker-pdf[full]：DOCX/PPTX/XLSX/HTML/EPUB")
    parser.add_argument("--gui", action="store_true", help="Streamlit GUI 依赖")
    parser.add_argument("--server", action="store_true", help="FastAPI/uvicorn HTTP 接口依赖")
    if with_json:
        parser.add_argument("--json", action="store_true", help="机器可读输出")
    return parser


def _features(ns) -> set:
    return {f for f in ("full", "gui", "server") if getattr(ns, f)}


def cmd_preflight(args) -> int:
    ns = _feature_parser("preflight", True).parse_args(args)
    return _report(collect_checks(resolve_venv(), _features(ns)), ns.json)


# ---------------------------------------------------------------------------
# 安装
# ---------------------------------------------------------------------------

def cmd_setup(args) -> int:
    ns = _feature_parser("setup", False).parse_args(args)
    features = _features(ns)
    venv_dir = resolve_venv()
    py = runtime.venv_exe(venv_dir, "python")

    if venv_dir.exists() and not venv_dir.is_dir():
        print(f"❌ {venv_dir} 已存在且不是目录；未做任何修改", file=sys.stderr)
        return 1
    is_empty = not venv_dir.exists() or not any(venv_dir.iterdir())
    if is_empty:
        if not _version_ok(sys.version_info):
            print(f"❌ 新建 Marker 环境需要 Python >=3.10,<4，当前 {sys.executable} 为 "
                  f"{sys.version.split()[0]}；未创建任何目录。改用：{_setup_hint(features)}", file=sys.stderr)
            return 1
        print(f"→ 创建独立 venv: {venv_dir}（{sys.executable}）")
        try:
            venv.EnvBuilder(with_pip=True, system_site_packages=False,
                            symlinks=(os.name != "nt")).create(str(venv_dir))
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"❌ 创建 venv 失败: {exc}", file=sys.stderr)
            return 1
    elif not (venv_dir / "pyvenv.cfg").is_file() or not py.exists():
        print(f"❌ {venv_dir} 非空且不是 venv；为保护其内容，未做任何修改。"
              f"请指定新路径（{VENV_ENV}）", file=sys.stderr)
        return 1

    try:
        probe = _probe(venv_dir, [])
    except RuntimeError as exc:
        print(f"❌ {exc}；禁止安装", file=sys.stderr)
        return 1
    problem = _isolation_problem(venv_dir, probe)
    if problem:
        print(f"❌ {problem}；禁止安装", file=sys.stderr)
        return 1
    if not _version_ok(probe["version"]):
        ver = ".".join(str(x) for x in probe["version"])
        print(f"❌ {py} 为 Python {ver}，Marker 要求 >=3.10,<4；该目录保留不动，请换新路径", file=sys.stderr)
        return 1

    specs = [s for f in ("full", "gui", "server") if f in features for s in EXTRA_SPECS[f]]
    install = [str(py), "-I", "-m", "pip", "install", "-r", str(REQUIREMENTS)] + specs
    print("→ " + " ".join(install))
    env = runtime.mgmt_env()
    if subprocess.call(install, env=env) != 0:
        print("❌ pip 安装失败（原始错误见上）；旧排版环境未受影响", file=sys.stderr)
        return 1
    print("→ pip check")
    if subprocess.call([str(py), "-I", "-m", "pip", "check"], env=env) != 0:
        print("❌ pip check 报告依赖冲突", file=sys.stderr)
        return 1
    print("→ preflight")
    code = _report(collect_checks(venv_dir, features), False)
    if code == 0:
        print("Python 依赖已就绪；模型在首次转换时下载，转换能力以实际 smoke 为准。")
    return code


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------

def _run_env(venv_dir: Path, op: str) -> dict:
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    extra = os.environ.get(EXTRA_PYTHONPATH_ENV)
    if extra:
        parts = [p for p in extra.split(os.pathsep) if p]
        bad = [p for p in parts if not os.path.isabs(p)]
        if bad:
            raise UsageError(f"{EXTRA_PYTHONPATH_ENV} 只接受绝对路径：{bad}")
        env["PYTHONPATH"] = os.pathsep.join(parts)
    env["VIRTUAL_ENV"] = str(venv_dir)
    env["PYTHONNOUSERSITE"] = "1"
    env["PATH"] = str(runtime.bin_dir(venv_dir)) + os.pathsep + env.get("PATH", "")
    return env



def cmd_run(op: str, args) -> int:
    if args and args[0] == "--":
        args = args[1:]
    venv_dir = resolve_venv()
    features = {op} if op in ("gui", "server") else set()
    failed = [c for c in collect_checks(venv_dir, features) if c["status"] == "fail"]
    if failed:
        print(f"❌ Marker 环境未就绪（{venv_dir}）：", file=sys.stderr)
        for c in failed:
            print(f"   - {c['name']}: {c['hint']}", file=sys.stderr)
        print(f"   修复：{_setup_hint(features)}（不会改动排版引擎环境）", file=sys.stderr)
        return 1
    env = _run_env(venv_dir, op)
    return runtime.run_owned([str(runtime.venv_exe(venv_dir, RUN_ENTRIES[op]))] + list(args), env)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    op, rest = argv[0], argv[1:]
    if op in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    try:
        if op == "setup":
            return cmd_setup(rest)
        if op == "preflight":
            return cmd_preflight(rest)
        if op in RUN_ENTRIES:
            return cmd_run(op, rest)
        print(f"未知操作 {op!r}\n\n{USAGE}", file=sys.stderr)
        return 2
    except UsageError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    except SystemExit as exc:  # argparse 参数错误/--help
        return exc.code if isinstance(exc.code, int) else 2


if __name__ == "__main__":
    sys.exit(main())
