from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "maintenance.py"
spec = importlib.util.spec_from_file_location("maintenance", SCRIPT)
maintenance = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(maintenance)


def test_report_covers_optional_marker_pins_and_metadata_failure(monkeypatch):
    def package(name, current, pinned):
        unavailable = name == "surya-ocr"
        return {"name": name, "source": "PyPI", "current": current, "pinned": pinned,
                "latest": None if unavailable else "9.0",
                "status": "unavailable" if unavailable else "update_available",
                "update_available": None if unavailable else True, "url": "https://pypi.org/"}
    monkeypatch.setattr(maintenance, "_requirements", lambda: [("markitdown", "0.1.8")])
    monkeypatch.setattr(maintenance, "_marker_requirements", lambda: [("marker-pdf", "2.0.0"), ("surya-ocr", "0.22.1")])
    monkeypatch.setattr(maintenance, "_current_versions", lambda: ({}, None))
    monkeypatch.setattr(maintenance, "_package_update", package)
    monkeypatch.setattr(maintenance, "_omp_update", lambda: {"name": "omp", "status": "available", "update_available": False})

    report = maintenance.check_updates()
    rows = {row["name"]: row for row in report["updates"]}

    assert report["schema_version"] == 1
    assert report["overall"] == "unavailable"
    assert rows["marker-pdf"]["pinned"] == "2.0.0"
    assert rows["surya-ocr"]["pinned"] == "0.22.1"
    assert rows["surya-ocr"]["status"] == "unavailable"
    assert rows["markitdown"]["update_available"] is True


def test_release_comparison_handles_equivalent_and_prerelease_versions():
    key = maintenance._version_key

    assert key("1.2") == key("1.2.0")
    assert key("1.2rc1") < key("1.2")
    assert key("1.2.dev1") < key("1.2a1")
    assert key("1.2") < key("1.2.post1")


def test_requirement_manifests_keep_pins_and_unpinned_runtime_packages(tmp_path):
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("Example_Pkg[extra]==1.2.3\nfloating-dependency\n", encoding="utf-8")

    assert maintenance._read_requirements(manifest) == [
        ("Example_Pkg", "1.2.3"), ("floating-dependency", None)
    ]


def test_init_skips_setup_for_healthy_environments_and_checks_requested_marker(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = '{"overall":"ok"}'
        stderr = ""

    monkeypatch.setattr(maintenance.subprocess, "run", lambda command, **kwargs: calls.append(command) or Result())

    assert maintenance._init(marker=True) == 0
    assert [call[-2:] for call in calls] == [
        ["preflight", "--json"], ["preflight", "--json"]
    ]
    assert calls[0][1] == str(maintenance.LIGHT_SETUP)
    assert calls[1][1] == str(maintenance.MARKER_SETUP)


def test_init_repairs_unhealthy_environment_then_rechecks(monkeypatch):
    calls = []
    outcomes = iter([1, 0, 0])

    class Result:
        stdout = ""
        stderr = ""

        def __init__(self, returncode):
            self.returncode = returncode

    def run(command, **kwargs):
        calls.append(command)
        return Result(next(outcomes))

    monkeypatch.setattr(maintenance.subprocess, "run", run)

    assert maintenance._init(marker=False) == 0
    assert [call[1:] for call in calls] == [
        [str(maintenance.LIGHT_SETUP), "preflight", "--json"],
        [str(maintenance.LIGHT_SETUP), "setup"],
        [str(maintenance.LIGHT_SETUP), "preflight", "--json"],
    ]


