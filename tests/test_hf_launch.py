"""Offline checks of the Hugging Face launcher: what is staged and what command runs (no Hub call)."""

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hf_launch", ROOT / "scripts" / "hf_launch.py")
hf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hf)

# Staging runs on the laptop only; the Job image has no git.
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _repo(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / ".gitignore").write_text("secret.txt\nstate/\n", encoding="utf-8")
    for f in ["harness/a.py", "scripts/b.py", "new_file.md", "secret.txt", "state/ledger.jsonl", ".venv/x.py"]:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    git("add", "harness/a.py", "scripts/b.py", ".gitignore")
    return tmp_path


@needs_git
def test_staging_takes_git_files_only(tmp_path):
    repo = _repo(tmp_path)
    files = hf.staged_files(repo)
    assert "harness/a.py" in files and "scripts/b.py" in files and "new_file.md" in files
    assert not any(f.startswith((".venv/", "state/")) or f == "secret.txt" for f in files)


@needs_git
def test_stage_code_recreates_the_folder(tmp_path):
    repo = _repo(tmp_path / "repo")
    dest = tmp_path / "stage"
    (dest / "stale.txt").parent.mkdir(parents=True)
    (dest / "stale.txt").write_text("old", encoding="utf-8")
    n = hf.stage_code(repo, dest)
    assert n == len(hf.staged_files(repo))
    assert (dest / "harness" / "a.py").exists() and not (dest / "stale.txt").exists()
    assert not (dest / "secret.txt").exists()


def test_run_command_runs_in_work_with_pinned_requirements():
    cmd = hf.job_command(["python", r"scripts\run_grid.py", "--n", "10"])
    assert cmd[:2] == ["bash", "-c"]
    script = cmd[2]
    assert script.startswith("set -euo pipefail; pip install") and "-r /code/requirements.txt" in script
    assert "export PYTHONPATH=/code" in script
    assert script.endswith("cd /work && python scripts/run_grid.py --n 10")


def test_selftest_puts_test_temp_folders_on_the_bucket():
    script = hf.job_command(None, selftest=True)[2]
    assert "--basetemp=/work/selftest-tmp" in script and "tee /work/selftest.txt" in script


def test_run_needs_a_command():
    with pytest.raises(ValueError):
        hf.job_command([])


def test_run_requires_an_explicit_timeout():
    with pytest.raises(SystemExit):
        hf.main(["run", "--", "python", "x.py"])
