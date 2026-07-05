"""Workspace inventory scanner."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .git import git_status
from .model import InventoryAsset
from .paths import current_placement, default_external_root

CONTAINER_NAMES = {"open-source", "worktrees", "forks", "sandbox"}


def scan_inventory(
    workspace_root: Path, external_root: Path | None = None
) -> list[InventoryAsset]:
    workspace_root = workspace_root.expanduser().resolve()
    assets: list[InventoryAsset] = []
    if workspace_root.exists():
        for path in _workspace_paths(workspace_root):
            assets.append(_asset_for_path(path, workspace_root))

    external = external_root or default_external_root()
    if external.exists():
        for path in _direct_children(external):
            assets.append(
                InventoryAsset(
                    kind="ExternalRef",
                    name=path.name,
                    path=path,
                    current_placement="external-readonly",
                    git=git_status(path),
                )
            )
    return sorted(assets, key=lambda item: (item.current_placement, item.name))


def _workspace_paths(workspace_root: Path) -> Iterable[Path]:
    for path in _direct_children(workspace_root):
        yield path
        if path.name in CONTAINER_NAMES:
            yield from _direct_children(path)


def _direct_children(path: Path) -> list[Path]:
    return sorted(
        child
        for child in path.iterdir()
        if child.is_dir() and not child.name.startswith(".")
    )


def _asset_for_path(path: Path, workspace_root: Path) -> InventoryAsset:
    placement = current_placement(path, workspace_root)
    kind = _kind_for_path(path, workspace_root, placement)
    labels: dict[str, str] = {}
    if placement == "workspace/worktrees":
        labels["worktree"] = path.name
    elif placement == "workspace/open-source":
        labels["repo"] = path.name
    return InventoryAsset(
        kind=kind,
        name=path.name,
        path=path,
        current_placement=placement,
        git=git_status(path),
        labels=labels,
    )


def _kind_for_path(path: Path, workspace_root: Path, placement: str) -> str:
    rel_parts = path.resolve().relative_to(workspace_root.resolve()).parts
    if len(rel_parts) == 1 and path.name in CONTAINER_NAMES:
        return "Container"
    if placement == "workspace/worktrees" and len(rel_parts) > 1:
        return "Worktree"
    if placement in {"workspace/open-source", "workspace/forks"} and len(rel_parts) > 1:
        return "Repo"
    if placement == "workspace/sandbox" and len(rel_parts) > 1:
        return "TaskContext"
    return "Repo" if (path / ".git").exists() else "Container"
