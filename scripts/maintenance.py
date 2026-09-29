#!/usr/bin/env python3
"""Initialize isolated 2md environments and report upstream version reminders."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
LIGHT_SETUP = SCRIPTS / "2md.py"
MARKER_SETUP = SCRIPTS / "marker_runner.py"
REQUIREMENTS = SCRIPTS / "requirements-markitdown.txt"
TIMEOUT = 15
AGENT = "2md-maintenance/1.0"


def _request_json(url: str) -> dict:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": AGENT})
    with urlopen(request, timeout=TIMEOUT) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        return json.load(response)


def _read_requirements(path: Path) -> list[tuple[str, str | None]]:
    packages = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.partition("#")[0].strip()
        match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?\s*(?:==\s*([^;\s]+))?", line)
        if match:
            packages.append((match.group(1), match.group(2)))
    return packages


def _requirements() -> list[tuple[str, str | None]]:
    return _read_requirements(REQUIREMENTS)


def _marker_requirements() -> list[tuple[str, str | None]]:
    return _read_requirements(SCRIPTS / "requirements-marker.txt")


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _version_key(version: str) -> tuple:
    match = re.fullmatch(
        r"\s*v?(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?(?:\.post(\d+))?(?:\.dev(\d+))?(?:\+[0-9a-z.-]+)?\s*",
        version, re.I,
    )
    if not match:
        raise ValueError(f"unrecognized version {version!r}")
    release = [int(part) for part in match.group(1).split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    pre_kind, pre_num, post_num, dev_num = match.group(2, 3, 4, 5)
    if pre_kind:
        stage = (1, {"a": 0, "b": 1, "rc": 2}[pre_kind.lower()], int(pre_num),
                 0 if dev_num is not None else 1, int(dev_num or 0))
    elif post_num is not None:
        stage = (3, int(post_num), 0 if dev_num is not None else 1, int(dev_num or 0))
    elif dev_num is not None:
        stage = (0, int(dev_num))
    else:
        stage = (2,)
    return tuple(release), stage


def _interpreter(env_name: str, default: Path) -> Path:
    raw = os.environ.get(env_name)
    if raw is None:
        root = default
    elif not raw.strip() or not Path(raw).expanduser().is_absolute():
        raise ValueError(f"{env_name} must be an absolute path when set")
    else:
        root = Path(raw).expanduser()
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _probe_versions(python: Path, names: list[str]) -> dict[str, str]:
    code = (
        "import importlib.metadata as m,json; names=" + repr(names) + "; out={}; "
        "\nfor n in names:\n"
        " try: out[n]=m.version(n)\n"
        " except m.PackageNotFoundError: pass\n"
        "print(json.dumps(out))"
    )
    result = subprocess.run([str(python), "-I", "-c", code], capture_output=True,
                            text=True, timeout=TIMEOUT, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"interpreter exited {result.returncode}")
    payload = json.loads(result.stdout)
    return {_canonical_name(name): str(version) for name, version in payload.items()}


def _current_versions() -> tuple[dict[str, str], str | None]:
    light = _interpreter("JZ2MD_VENV", Path.home() / ".venvs" / "2md")
    marker = _interpreter("JZ2MD_MARKER_VENV", Path.home() / ".venvs" / "2pdf-marker")
    try:
        versions = {}
        if light.is_file():
            versions.update(_probe_versions(light, [name for name, _ in _requirements()]))
        if marker.is_file():
            versions.update(_probe_versions(marker, [name for name, _ in _marker_requirements()]))
        return versions, None
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return {}, str(exc)


def _package_update(name: str, current: str | None, pinned: str | None) -> dict:
    item = {"name": name, "source": "PyPI", "current": current, "pinned": pinned,
            "latest": None, "status": "unavailable", "update_available": None,
            "url": f"https://pypi.org/project/{name}/"}
    try:
        latest = str(_request_json(f"https://pypi.org/pypi/{name}/json")["info"]["version"])
        baseline = current or pinned
        if baseline is None:
            raise RuntimeError("no installed version or pinned baseline is available")
        newer = _version_key(latest) > _version_key(baseline)
        item.update(latest=latest, status="update_available" if newer else "available",
                    update_available=bool(newer))
    except (OSError, URLError, HTTPError, TimeoutError, RuntimeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        item["detail"] = str(exc)
    return item


def _omp_update() -> dict | None:
    executable = shutil.which("omp")
    if not executable:
        return None
    item = {"name": "omp", "source": "GitHub Releases", "current": None,
            "pinned": None, "latest": None, "status": "unavailable",
            "update_available": None,
            "url": "https://github.com/can1357/oh-my-pi/releases"}
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                timeout=TIMEOUT, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"omp exited {result.returncode}")
        match = re.search(r"\bv?(\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?)\b", result.stdout + "\n" + result.stderr)
        if not match:
            raise ValueError("omp --version did not report a version")
        current = match.group(1)
        release = _request_json("https://api.github.com/repos/can1357/oh-my-pi/releases/latest")
        latest = str(release.get("tag_name", "")).lstrip("v")
        if not latest:
            raise ValueError("latest release has no tag_name")
        newer = _version_key(latest) > _version_key(current)
        item.update(current=current, latest=latest,
                    status="update_available" if newer else "available",
                    update_available=bool(newer))
    except (OSError, subprocess.SubprocessError, URLError, HTTPError, TimeoutError, RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        item["detail"] = str(exc)
    return item


def check_updates() -> dict:
    versions, probe_error = _current_versions()
    package_specs = _requirements() + _marker_requirements()
    package_jobs = [(name, versions.get(_canonical_name(name)), pin)
                    for name, pin in package_specs]
    with ThreadPoolExecutor(max_workers=min(8, len(package_jobs) + 1)) as pool:
        futures = [pool.submit(_package_update, *job) for job in package_jobs]
        updates = [future.result() for future in futures]
        omp_future = pool.submit(_omp_update)
        omp_item = omp_future.result()
    if omp_item is not None:
        updates.append(omp_item)
    if probe_error:
        for item in updates:
            if item["current"] is None and item["name"] != "omp":
                item.update(status="unavailable", update_available=None)
                item["detail"] = "installed version unavailable: " + probe_error
    return {"schema_version": 1,
            "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "overall": "unavailable" if any(row["status"] == "unavailable" for row in updates) else "ok",
            "updates": updates}


def _run(command: list[str]) -> int:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
    return result.returncode


def _ensure(script: Path) -> int:
    command = [sys.executable, str(script)]
    if _run(command + ["preflight", "--json"]) == 0:
        return 0
    if _run(command + ["setup"]) != 0:
        return 1
    return _run(command + ["preflight", "--json"])


def _init(marker: bool) -> int:
    scripts = [LIGHT_SETUP]
    if marker:
        scripts.append(MARKER_SETUP)
    for script in scripts:
        if _ensure(script) != 0:
            return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="maintenance.py")
    commands = parser.add_subparsers(dest="command", required=True)
    init_parser = commands.add_parser("init", help="initialize isolated 2md environments")
    init_parser.add_argument("--marker", action="store_true", help="also initialize the separate Marker core environment")
    updates_parser = commands.add_parser("check-updates", help="check official upstream version metadata")
    updates_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    if args.command == "init":
        return _init(args.marker)
    report = check_updates()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    else:
        for row in report["updates"]:
            current = row["current"] or (f"pin {row['pinned']}" if row["pinned"] else "not installed")
            latest = row["latest"] or "unavailable"
            action = "update available" if row["status"] == "update_available" else row["status"]
            print(f"{row['name']}: current {current}; latest {latest}; {action} — {row['url']}")
            if row.get("detail"):
                print(f"  {row['detail']}")
    return 1 if report["overall"] == "unavailable" else 0


if __name__ == "__main__":
    sys.exit(main())
