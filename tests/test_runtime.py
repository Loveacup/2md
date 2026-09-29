"""Consumer-visible launcher isolation: real interpreters, temporary HOME, no installs."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

RUNNER = Path(__file__).resolve().parents[1] / 'scripts' / '2md.py'


def run_cli(home, *args, extra=None):
    env = {k: v for k, v in os.environ.items() if k not in ('JZ2MD_VENV', 'JZ2MD_MARKER_VENV', 'PYTHONPATH', 'PYTHONHOME')}
    env.update(HOME=str(home), USERPROFILE=str(home))
    env.update(extra or {})
    return subprocess.run([sys.executable, '-B', str(RUNNER), *args], env=env, capture_output=True, text=True, timeout=40)


def test_missing_preflight_never_creates_environment(tmp_path):
    absent = tmp_path / 'absent'
    p = run_cli(tmp_path, 'preflight', '--json', extra={'JZ2MD_VENV': str(absent)})
    assert p.returncode == 1
    assert json.loads(p.stdout)['overall'] == 'fail'
    assert not absent.exists()


@pytest.mark.parametrize('relative', ['.venvs/pdf-skill', '.venvs/pdf-skill/nested', '.venvs', '.venvs/2pdf-marker', '.venvs/2pdf-marker/nested'])
def test_overlap_cannot_modify_environments(tmp_path, relative):
    legacy = tmp_path / '.venvs' / 'pdf-skill'
    legacy.mkdir(parents=True)
    sentinel = legacy / 'keep'
    sentinel.write_bytes(b'original')
    p = run_cli(tmp_path, 'setup', extra={'JZ2MD_VENV': str(tmp_path / relative)})
    assert p.returncode == 2
    assert sentinel.read_bytes() == b'original'
    assert sorted(x.relative_to(tmp_path).as_posix() for x in tmp_path.rglob('*')) == ['.venvs', '.venvs/pdf-skill', '.venvs/pdf-skill/keep']


def test_custom_marker_overlap_is_refused(tmp_path):
    marker = tmp_path / 'custom-marker'
    p = run_cli(tmp_path, 'setup', extra={'JZ2MD_VENV': str(marker / 'nested'), 'JZ2MD_MARKER_VENV': str(marker)})
    assert p.returncode == 2
    assert not marker.exists()


def test_nonvenv_is_preserved(tmp_path):
    target = tmp_path / 'important'
    target.mkdir()
    f = target / 'notes'
    f.write_bytes(b'do not overwrite')
    digest = hashlib.sha256(f.read_bytes()).hexdigest()
    p = run_cli(tmp_path, 'setup', extra={'JZ2MD_VENV': str(target)})
    assert p.returncode == 1
    assert hashlib.sha256(f.read_bytes()).hexdigest() == digest
    assert list(target.iterdir()) == [f]


@pytest.mark.skipif(os.name == 'nt', reason='POSIX interpreter wrapper')
def test_spoofed_prefix_never_runs_pip(tmp_path):
    actual = tmp_path / 'actual'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(actual)], check=True)
    fake = tmp_path / 'fake'
    (fake / 'bin').mkdir(parents=True)
    (fake / 'pyvenv.cfg').write_text('include-system-site-packages = false\n')
    log = tmp_path / 'calls'
    py = fake / 'bin' / 'python'
    py.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexec "{actual}/bin/python" "$@"\n')
    py.chmod(0o700)
    p = run_cli(tmp_path, 'setup', extra={'JZ2MD_VENV': str(fake)})
    assert p.returncode == 1
    assert 'prefix' in p.stderr
    assert '-m pip' not in log.read_text()


@pytest.mark.parametrize('extra', [{'JZ2MD_VENV': ''}, {'JZ2MD_MARKER_VENV': ''}])
def test_empty_environment_paths_are_usage_errors(tmp_path, extra):
    p = run_cli(tmp_path, 'preflight', extra=extra)
    assert p.returncode == 2
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('args', [
    ['--vision-backend', 'openai-compatible'],
    ['--vision-backend', 'openai-compatible', '--base-url', 'https://example.invalid/v1'],
    ['--vision-backend', 'openai-compatible', '--base-url', 'https://user:secret@example.invalid/v1', '--vision-model', 'model'],
    ['--vision-backend', 'openai-compatible', '--base-url', 'https://example.invalid/v1?key=secret', '--vision-model', 'model'],
    ['--vision-backend', 'openai-compatible', '--base-url', 'file:///tmp/model', '--vision-model', 'model'],
])
def test_invalid_vision_configuration_precedes_environment_probe(tmp_path, args):
    p = run_cli(tmp_path, 'preflight', *args)
    assert p.returncode == 2
    assert not list(tmp_path.iterdir())
    assert 'secret' not in p.stderr
