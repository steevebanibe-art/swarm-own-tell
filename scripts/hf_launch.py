"""Run a command of this repository as a Hugging Face Job (generic launcher).

PRE-EXISTING GENERIC INFRASTRUCTURE - written on 4 Nov 2026, before the Swarm
Alignment Hackathon, and declared in the report. Nothing project-specific.

How it works
* Code: the files git knows about (tracked, or new and not ignored) are copied
  to ``.hf_stage/code`` and synced to the private ``<user>/jobs-artifacts``
  bucket, then mounted read-only at ``/code``. Never ``.venv``, ``state/``,
  ``runs/`` or anything in .gitignore.
* Work: a bucket folder mounted read-write at ``/work``; the command runs there,
  so ``state/`` (ledger, caches) and ``runs/`` live in the bucket and survive
  the Job. Nothing local is ever uploaded to it (a local copy could overwrite
  newer files written by a Job). ``pull`` copies it down to ``hf_work/``.
* The Anthropic key is sent only with ``--with-anthropic-key``, as an encrypted
  Job secret read from the environment (or the Windows user variable). It is
  never printed or written.
* One writer per ledger: never run the same queue on the laptop and in a Job.

Usage (PowerShell, from the repository folder):
  .venv\\Scripts\\python.exe scripts\\hf_launch.py selftest
  .venv\\Scripts\\python.exe scripts\\hf_launch.py run --timeout 10h --with-anthropic-key -- python scripts\\x.py
  .venv\\Scripts\\python.exe scripts\\hf_launch.py status JOB_ID
  .venv\\Scripts\\python.exe scripts\\hf_launch.py logs JOB_ID [--follow]
  .venv\\Scripts\\python.exe scripts\\hf_launch.py cancel JOB_ID
  .venv\\Scripts\\python.exe scripts\\hf_launch.py pull
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / ".hf_stage"
LOCAL_WORK = ROOT / "hf_work"
CODE_REMOTE = "swarm-own-tell-code"
WORK_REMOTE = "swarm-own-tell-work"
IMAGE = "python:3.13-slim"
NEVER_STAGE = (".venv/", "state/", "runs/", "hf_work/", ".hf_stage/", ".git/")


def staged_files(repo: Path) -> list[str]:
    """Files git tracks or would track (not ignored), minus the never-staged folders."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    files = sorted({f for f in out.split("\0") if f})
    return [f for f in files if not f.startswith(NEVER_STAGE) and (repo / f).is_file()]


def stage_code(repo: Path, dest: Path) -> int:
    """Recreate ``dest`` with a copy of the staged files; returns how many were copied."""
    if dest.exists():
        shutil.rmtree(dest)
    files = staged_files(repo)
    for f in files:
        target = dest / f
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / f, target)
    return len(files)


def job_command(user_cmd: list[str] | None, *, selftest: bool = False) -> list[str]:
    """The bash command run in the Job: install pinned requirements, then the command in /work."""
    setup = "set -euo pipefail; pip install --quiet --no-cache-dir --disable-pip-version-check -r /code/requirements.txt"
    if selftest:
        # The test suite runs with its temporary folders on the mounted bucket, so file locks,
        # fsync'd appends and atomic replaces are exercised on the storage the real runs will use.
        body = (
            "cd /code && python -m pytest -p no:cacheprovider --basetemp=/work/selftest-tmp "
            "2>&1 | tee /work/selftest.txt"
        )
    else:
        if not user_cmd:
            raise ValueError("a command is required after --")
        cmd = [c.replace("\\", "/") for c in user_cmd]  # Windows paths typed locally
        body = "cd /work && " + shlex.join(cmd)
    return ["bash", "-c", f"{setup} && export PYTHONPATH=/code && {body}"]


def anthropic_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key and sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                key = winreg.QueryValueEx(k, "ANTHROPIC_API_KEY")[0]
        except OSError:
            key = None
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY not found in the environment or the Windows user variables")
    return key


def private_bucket(api) -> str:
    """The ``<user>/jobs-artifacts`` bucket, created private if missing; stops if it is public. Uploads nothing."""
    from huggingface_hub import constants

    bucket = f"{api.whoami()['name']}/{constants.HF_JOBS_ARTIFACTS_BUCKET_NAME}"
    api.create_bucket(bucket, exist_ok=True, private=True)
    if not api.bucket_info(bucket).private:
        raise SystemExit(f"[hf] bucket {bucket} is PUBLIC: make it private on the Hub first. Nothing was uploaded.")
    return bucket


def volumes(api):
    """(code volume, work volume). The privacy check runs before anything is uploaded."""
    private_bucket(api)
    n = stage_code(ROOT, STAGE / "code")
    print(f"[hf] {n} files staged for /code")
    code = api.sync_job_volume(STAGE / "code", "/code", remote_name=CODE_REMOTE, read_only=True)
    empty = STAGE / "work-empty"
    if empty.exists():
        shutil.rmtree(empty)
    empty.mkdir(parents=True)
    # An empty folder uploads nothing (only a .keep placeholder); files already in the bucket stay.
    work = api.sync_job_volume(empty, "/work", remote_name=WORK_REMOTE, read_only=False)
    return code, work


def log_job(job, kind: str, cmd: list[str], timeout: str, flavor: str, with_key: bool) -> None:
    LOCAL_WORK.mkdir(exist_ok=True)
    rec = {
        "time_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "id": job.id,
        "url": job.url,
        "kind": kind,
        "command": cmd,
        "timeout": timeout,
        "flavor": flavor,
        "anthropic_key_sent": with_key,
    }
    with open(LOCAL_WORK / "jobs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def launch(args, kind: str) -> None:
    from huggingface_hub import HfApi

    api = HfApi()
    user = api.whoami()["name"]
    code, work = volumes(api)
    cmd = job_command(args.cmd, selftest=(kind == "selftest"))
    secrets = {"ANTHROPIC_API_KEY": anthropic_key()} if args.with_anthropic_key else None
    job = api.run_job(
        image=IMAGE,
        command=cmd,
        env={"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"},
        secrets=secrets,
        flavor=args.flavor,
        timeout=args.timeout,
        name=f"swarm-{kind}",
        volumes=[code, work],
    )
    log_job(job, kind, cmd, args.timeout, args.flavor, bool(secrets))
    print(f"[hf] job {job.id} started as {user} on {args.flavor}, timeout {args.timeout}")
    print(f"[hf] {job.url}")
    print("[hf] Anthropic key sent as an encrypted secret" if secrets else "[hf] no API key sent")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="action", required=True)
    st = sub.add_parser("selftest", help="run the offline test suite in a Job (no API key sent)")
    st.add_argument("--timeout", default="30m")
    st.add_argument("--flavor", default="cpu-basic")
    r = sub.add_parser("run", help="run a command in /work")
    r.add_argument("--timeout", required=True, help='e.g. "10h"; the Hub default is only 30 minutes')
    r.add_argument("--flavor", default="cpu-basic")
    r.add_argument("--with-anthropic-key", action="store_true")
    r.add_argument("cmd", nargs=argparse.REMAINDER)
    for name in ("status", "logs", "cancel"):
        s = sub.add_parser(name)
        s.add_argument("job_id")
        if name == "logs":
            s.add_argument("--follow", action="store_true")
    sub.add_parser("pull", help="copy the /work bucket folder down to hf_work/")
    args = p.parse_args(argv)

    if args.action == "selftest":
        args.cmd, args.with_anthropic_key = None, False
        launch(args, "selftest")
        return
    if args.action == "run":
        if args.cmd and args.cmd[0] == "--":
            args.cmd = args.cmd[1:]
        launch(args, "run")
        return

    from huggingface_hub import HfApi

    api = HfApi()
    if args.action == "status":
        info = api.inspect_job(job_id=args.job_id)
        print(info.status.stage, info.status.message or "", info.url)
    elif args.action == "logs":
        for line in api.fetch_job_logs(job_id=args.job_id, follow=args.follow):
            print(line, flush=True)
    elif args.action == "cancel":
        api.cancel_job(job_id=args.job_id)
        print(f"[hf] job {args.job_id} cancelled")
    elif args.action == "pull":
        bucket = private_bucket(api)
        LOCAL_WORK.mkdir(exist_ok=True)
        api.sync_bucket(f"hf://buckets/{bucket}/{WORK_REMOTE}", str(LOCAL_WORK / "work"), quiet=True)
        print(f"[hf] /work copied to {LOCAL_WORK / 'work'}")


if __name__ == "__main__":
    main()
