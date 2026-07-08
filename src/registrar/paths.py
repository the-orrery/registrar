"""Path defaults and placement helpers."""

from __future__ import annotations

import os
from pathlib import Path


def expand(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def default_workspace_root() -> Path:
    raw = os.environ.get("REGISTRAR_WORKSPACE_ROOT")
    return expand(raw) if raw else (Path.home() / "workspace").resolve()


def default_external_root() -> Path:
    raw = os.environ.get("REGISTRAR_EXTERNAL_ROOT")
    return expand(raw) if raw else (Path.home() / "external-readonly").resolve()


def default_archive_root() -> Path:
    raw = os.environ.get("REGISTRAR_ARCHIVE_ROOT")
    return expand(raw) if raw else (Path.home() / "workspace-archive").resolve()


def default_registry_root() -> Path:
    raw = os.environ.get("REGISTRAR_REGISTRY_ROOT")
    return expand(raw) if raw else (Path.home() / ".registrar" / "registry").resolve()


def current_placement(path: Path, workspace_root: Path) -> str:
    path = path.resolve()
    workspace_root = workspace_root.resolve()
    try:
        rel = path.relative_to(workspace_root)
    except ValueError:
        external = default_external_root()
        archive = default_archive_root()
        if _is_relative_to(path, external):
            return "external-readonly"
        if _is_relative_to(path, archive):
            return "archive"
        return "unknown"

    if not rel.parts:
        return "workspace/root"
    parent = rel.parent
    if parent == Path():
        return "workspace/root"
    return f"workspace/{parent.as_posix()}"


def target_for_placement(
    name: str,
    placement: str,
    workspace_root: Path,
    external_root: Path | None = None,
    archive_root: Path | None = None,
) -> Path:
    if placement == "workspace/root":
        return workspace_root / name
    if placement.startswith("workspace/"):
        return workspace_root / placement.removeprefix("workspace/") / name
    if placement == "external-readonly":
        return (external_root or default_external_root()) / name
    if placement == "archive":
        return (archive_root or default_archive_root()) / name
    return workspace_root / name


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
