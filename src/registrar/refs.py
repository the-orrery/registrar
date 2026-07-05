"""Scan known local ref sources for references to a repo path.

Answers: if this repo moves, what else points at it? Checks launchd plists,
user-local symlinks/shims, text config under configured control-plane roots,
the dotfiles repo, an optional source index, and shell rc files — matching every textual form a ref might use
(absolute, ``$HOME``/``~`` relative, and workspace-relative).

Each ref is classified ``rewrite`` (a live functional pointer relocate may
edit) or ``preserve`` (historical / generated / frozen content relocate must
NOT touch: PM data, archives, runtime records, ``.md`` docs). The scan is
read-only; ``apply`` consumes the classification.

``scan_workspace_sweep_refs`` is the broader safety net: it scans the whole
workspace tree, excluding the default control-plane roots already covered by
``scan_affected_refs``. It is report-only and never consumed by ``apply``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

from .model import Ref
from .paths import default_archive_root

_MAX_FILE_BYTES = 1_000_000
# Characters that, if adjacent to a path expression, mean it is a different/longer
# leaf ("foo" vs "foobar"). "/" is excluded on purpose: a trailing slash is a
# deeper path *inside* the repo and should still match.
#
# Left and right boundaries are asymmetric because shell default expressions use
# ":-${HOME}/path"; the "-" before a ${HOME} path is an operator, not a path
# leaf character. Keep "-" on the right boundary to avoid matching foo in
# foo-bar.
_LEFT_BOUNDARY_CHAR = r"A-Za-z0-9_."
_RIGHT_BOUNDARY_CHAR = r"A-Za-z0-9_.\-"
_SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    # high-volume generated/cache trees: never functional config, pure scan noise
    "file-history",
    "shell-snapshots",
    "Cache",
    "Caches",
    "logs",
}

# Path segments that mark generated/historical content relocate must not rewrite.
_PRESERVE_PARTS = {
    "runtime",
    "audits",
    "captures",
    ".archive",
}
# Suffixes/basenames that mark a live, machine-read config file (safe to rewrite).
_FUNCTIONAL_SUFFIXES = {
    ".toml",
    ".json",
    ".plist",
    ".sh",
    ".bash",
    ".zsh",
    ".yaml",
    ".yml",
    ".cfg",
    ".ini",
    ".conf",
    ".env",
    ".py",
}
_FUNCTIONAL_BASENAMES = {
    ".zshrc",
    ".zprofile",
    ".zshenv",
    ".bashrc",
    ".bash_profile",
    ".profile",
}


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def default_scan_roots(workspace_root: Path) -> list[Path]:
    """Default ref sources spanning the known control-plane config homes.

    These defaults are intentionally generic. Deployments with a richer local
    control plane should set REGISTRAR_REF_SCAN_ROOTS explicitly.
    """
    home = _home()
    return [
        home / "Library" / "LaunchAgents",
        home / ".local" / "bin",
        home / ".config",
        home / "host-config" / "dotfiles",
        workspace_root / "control-plane",
        workspace_root / "host-config",
        workspace_root / "knowledge" / "base" / "sources.toml",
        home / ".zshrc",
        home / ".zprofile",
        home / ".zshenv",
    ]


def path_ref_regex(token: str) -> re.Pattern[str]:
    """Match ``token`` only at path-expression boundaries.

    Prevents the substring trap where relocating ``foo`` would otherwise match
    (and corrupt) refs to a sibling ``foobar``: the match must not be flanked by
    another leaf-name character. A following ``/`` is allowed so deeper paths
    inside the repo still match.
    """
    left_chars = _LEFT_BOUNDARY_CHAR
    if _is_workspace_relative_token(token):
        left_chars += "/"
    return re.compile(
        rf"(?<![{left_chars}]){re.escape(token)}(?![{_RIGHT_BOUNDARY_CHAR}])"
    )


def _is_workspace_relative_token(token: str) -> bool:
    return (
        os.sep in token
        and not token.startswith(os.sep)
        and not token.startswith("~")
        and not token.startswith("$")
    )


def _under_preserve(source: Path) -> bool:
    if set(source.parts) & _PRESERVE_PARTS:
        return True
    try:
        source.resolve().relative_to(default_archive_root())
    except ValueError:
        return False
    return True


def classify_ref(source: Path, kind: str) -> str:
    """``rewrite`` for live functional pointers, ``preserve`` otherwise.

    Symlinks are functional pointers by nature (a dangling link is a bug), so
    they rewrite unless they live under preserved content. Text refs rewrite
    only when the file is a recognised machine-read config; ``.md`` docs and
    anything unrecognised default to ``preserve`` for manual review, so the tool
    never silently rewrites narrative/history (e.g. catalog dated changelogs).
    """
    if _under_preserve(source):
        return "preserve"
    if kind == "symlink":
        return "rewrite"
    if source.suffix == ".md":
        return "preserve"
    if source.name in _FUNCTIONAL_BASENAMES:
        return "rewrite"
    if source.parent.name == "bin":
        return "rewrite"
    if source.parent == _home() / ".local" / "bin":
        return "rewrite"
    if source.suffix in _FUNCTIONAL_SUFFIXES:
        return "rewrite"
    return "preserve"


def _env_scan_roots() -> list[Path] | None:
    raw = os.environ.get("REGISTRAR_REF_SCAN_ROOTS")
    if not raw:
        return None
    return [Path(part).expanduser() for part in raw.split(":") if part]


def path_variants(repo_path: Path, workspace_root: Path) -> list[str]:
    """Every textual form a ref to ``repo_path`` might take."""
    text = str(repo_path)
    variants = {text}
    home = str(_home())
    if text == home or text.startswith(home + os.sep):
        rel = text[len(home) :]
        variants.add("${HOME}" + rel)
        variants.add("$HOME" + rel)
        variants.add("~" + rel)
    workspace = str(workspace_root)
    if text.startswith(workspace + os.sep):
        rel = text[len(workspace) + 1 :]
        # Skip a bare leaf (e.g. "memex"): too generic a token to match or
        # rewrite safely — it collides with substrings of the new path and of
        # unrelated lines. Only multi-segment workspace-relative refs qualify.
        if os.sep in rel:
            variants.add(rel)
    return sorted(variants)


def scan_affected_refs(
    repo_path: Path,
    workspace_root: Path,
    scan_roots: Iterable[Path] | None = None,
) -> list[Ref]:
    repo_path = repo_path.resolve()
    if scan_roots is not None:
        roots = list(scan_roots)
    else:
        roots = _env_scan_roots() or default_scan_roots(workspace_root)
    patterns = [path_ref_regex(v) for v in path_variants(repo_path, workspace_root)]
    refs: list[Ref] = []
    seen: set[str] = set()
    for root in roots:
        for ref in _scan_root(root, repo_path, patterns):
            key = str(ref.source)
            if key not in seen:
                seen.add(key)
                refs.append(ref)
    return refs


def scan_workspace_sweep_refs(
    repo_path: Path,
    workspace_root: Path,
    exclude_roots: Sequence[Path] = (),
) -> list[Ref]:
    """Report refs found only by a broad workspace sweep.

    This intentionally excludes the default scanned roots so relocate dry-runs
    can show the extra TASK-549 findings separately. The caller must treat these
    refs as review-only; ``apply`` must not rewrite from this broad list.
    """
    workspace_root = workspace_root.resolve()
    excluded = _workspace_excluded_roots(
        workspace_root, (*default_scan_roots(workspace_root), *exclude_roots)
    )
    return [
        ref
        for ref in scan_affected_refs(
            repo_path, workspace_root, scan_roots=[workspace_root]
        )
        if not any(_is_same_or_under(ref.source.resolve(), root) for root in excluded)
    ]


def _workspace_excluded_roots(
    workspace_root: Path, roots: Iterable[Path]
) -> tuple[Path, ...]:
    return tuple(
        root.resolve()
        for root in roots
        if _is_same_or_under(root.resolve(), workspace_root)
    )


def _iter_files(root: Path) -> Iterator[Path]:
    if root.is_symlink() or root.is_file():
        yield root
        return
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".archive")
        ]
        for name in filenames:
            yield Path(dirpath) / name


def _scan_root(
    root: Path, repo_path: Path, patterns: list[re.Pattern[str]]
) -> Iterator[Ref]:
    for entry in _iter_files(root):
        if entry.is_symlink():
            ref = _symlink_ref(entry, repo_path)
            if ref is not None:
                yield ref
            continue
        ref = _text_ref(entry, patterns)
        if ref is not None:
            yield ref


def _symlink_ref(link: Path, repo_path: Path) -> Ref | None:
    try:
        raw = os.readlink(link)
    except OSError:
        return None
    target = Path(raw) if os.path.isabs(raw) else link.parent / raw
    target = Path(os.path.normpath(target))
    if _is_under(target, repo_path):
        return Ref(
            source=link,
            kind="symlink",
            detail=f"-> {raw}",
            category=classify_ref(link, "symlink"),
        )
    return None


def _text_ref(path: Path, patterns: list[re.Pattern[str]]) -> Ref | None:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    matches = [
        line.strip()
        for line in text.splitlines()
        if any(pattern.search(line) for pattern in patterns)
    ]
    if not matches:
        return None
    kind = "launchd" if path.suffix == ".plist" else "text"
    detail = matches[0][:200]
    if len(matches) > 1:
        detail = f"{len(matches)} matches; {detail}"
    return Ref(
        source=path,
        kind=kind,
        detail=detail,
        category=classify_ref(path, kind),
    )


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return path == root
    return True


def _is_same_or_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return path == root
    return True
