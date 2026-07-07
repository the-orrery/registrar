"""Small git probes used by inventory and finalizers."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .model import GitStatus


def git_status(path: Path) -> GitStatus:
    if not path.exists():
        return GitStatus(is_repo=False, branch="", dirty=False, untracked_count=0)

    status = _git(path, "status", "--porcelain=v1", "--branch", "--untracked-files=normal")
    if status.returncode != 0:
        return GitStatus(is_repo=False, branch="", dirty=False, untracked_count=0)

    lines = [line for line in status.stdout.splitlines() if line]
    branch = _branch_from_status(lines[0]) if lines else ""
    changed = [line for line in lines if not line.startswith("##")]
    return GitStatus(
        is_repo=True,
        branch=branch,
        dirty=any(not line.startswith("?? ") for line in changed),
        untracked_count=sum(1 for line in changed if line.startswith("?? ")),
    )


def commit_registry_change(path: Path, message: str) -> None:
    """Best-effort commit of a single registry record file.

    Pathspec-limited (only `path` is staged and committed) so unrelated working-tree
    drift is never swept into the commit. No-op when the registry is not inside a git
    work tree, or when the path has no staged change.
    """
    repo_dir = path.parent
    inside = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "--is-inside-work-tree"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return
    subprocess.run(
        ["git", "-C", str(repo_dir), "add", "--", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-q", "-m", message, "--", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def _branch_from_status(line: str) -> str:
    if not line.startswith("## "):
        return ""
    branch = line.removeprefix("## ").split("...", maxsplit=1)[0]
    return "" if branch.startswith("HEAD ") else branch
