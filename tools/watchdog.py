#!/usr/bin/env python3
"""Dispatch the scan workflow when GitHub's scheduled runs fall behind."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

ACTIVE_STATUSES = {"in_progress", "pending", "queued", "requested", "waiting"}


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def evaluate_runs(
    runs: list[dict],
    now: datetime,
    max_age: timedelta,
    retry_cooldown: timedelta,
) -> tuple[str, str]:
    active = next((run for run in runs if run.get("status") in ACTIVE_STATUSES), None)
    if active:
        return "skip", f"scan already {active['status']}: {active.get('url', '')}"

    successful = next((run for run in runs if run.get("conclusion") == "success"), None)
    if successful:
        completed_at = parse_timestamp(successful["updatedAt"])
        age = now - completed_at
        if age <= max_age:
            return (
                "skip",
                f"latest successful scan is {age.total_seconds() / 60:.0f}m old",
            )

    if runs:
        latest_attempt = max(parse_timestamp(run["createdAt"]) for run in runs)
        attempt_age = now - latest_attempt
        if attempt_age < retry_cooldown:
            return (
                "skip",
                f"latest scan attempt is only {attempt_age.total_seconds() / 60:.0f}m old",
            )

    if successful:
        completed_at = parse_timestamp(successful["updatedAt"])
        age_minutes = (now - completed_at).total_seconds() / 60
        return "dispatch", f"latest successful scan is stale ({age_minutes:.0f}m old)"
    return "dispatch", "no successful scan was found"


def find_gh(explicit: str | None) -> str:
    if explicit:
        return explicit
    configured = os.environ.get("GH_BIN")
    if configured:
        return configured
    found = shutil.which("gh")
    if found:
        return found
    for candidate in ("/usr/bin/gh", "/usr/local/bin/gh"):
        if Path(candidate).is_file():
            return candidate
    raise RuntimeError("GitHub CLI not found; set GH_BIN to its absolute path")


def run_gh(gh_bin: str, *args: str) -> str:
    result = subprocess.run(
        [gh_bin, *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def load_runs(gh_bin: str, repo: str, workflow: str) -> list[dict]:
    output = run_gh(
        gh_bin,
        "run",
        "list",
        "--repo",
        repo,
        "--workflow",
        workflow,
        "--limit",
        "20",
        "--json",
        "status,conclusion,createdAt,updatedAt,url,event",
    )
    return json.loads(output)


@contextmanager
def exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def log(message: str):
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{timestamp} {message}", flush=True)


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="Dispatch the scan workflow if its last success is too old."
    )
    parser.add_argument(
        "--repo", default=os.environ.get("GITHUB_REPOSITORY", "la55u/japan-trips")
    )
    parser.add_argument("--workflow", default="scan.yml")
    parser.add_argument("--ref", default="main")
    parser.add_argument("--max-age-minutes", type=int, default=75)
    parser.add_argument("--retry-cooldown-minutes", type=int, default=30)
    parser.add_argument("--gh-bin", default=None)
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=Path(f"/tmp/japan-trips-watchdog-{os.getuid()}.lock"),
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.max_age_minutes <= 0 or args.retry_cooldown_minutes < 0:
        raise ValueError("age thresholds must be positive")

    with exclusive_lock(args.lock_file) as acquired:
        if not acquired:
            log("another watchdog process is active; skipping")
            return 0

        gh_bin = find_gh(args.gh_bin)
        runs = load_runs(gh_bin, args.repo, args.workflow)
        action, reason = evaluate_runs(
            runs,
            datetime.now(UTC),
            timedelta(minutes=args.max_age_minutes),
            timedelta(minutes=args.retry_cooldown_minutes),
        )
        if action == "skip":
            log(reason)
            return 0
        if args.dry_run:
            log(f"dry run: would dispatch {args.workflow}: {reason}")
            return 0

        run_gh(
            gh_bin,
            "workflow",
            "run",
            args.workflow,
            "--repo",
            args.repo,
            "--ref",
            args.ref,
        )
        log(f"dispatched {args.workflow}: {reason}")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        ValueError,
        subprocess.CalledProcessError,
    ) as e:
        log(f"ERROR: {e}")
        raise SystemExit(1) from e
