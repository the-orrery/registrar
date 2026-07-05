from pathlib import Path

from registrar.inventory import scan_inventory
from registrar.paths import (
    current_placement,
    default_registry_root,
    target_for_placement,
)


def test_scan_inventory_classifies_workspace_containers(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "open-source" / "docket").mkdir(parents=True)
    (workspace / "worktrees" / "docket-123").mkdir(parents=True)
    (workspace / "sandbox" / "scratch").mkdir(parents=True)

    assets = scan_inventory(workspace, external_root=tmp_path / "external")
    by_name = {asset.name: asset for asset in assets}

    assert by_name["open-source"].kind == "Container"
    assert by_name["docket"].kind == "Repo"
    assert by_name["docket"].current_placement == "workspace/open-source"
    assert by_name["docket-123"].kind == "Worktree"
    assert by_name["scratch"].kind == "TaskContext"


def test_placement_helpers(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    path = workspace / "worktrees" / "issue-branch"

    assert current_placement(path, workspace) == "workspace/worktrees"
    assert target_for_placement("foo", "workspace/open-source", workspace) == (
        workspace / "open-source" / "foo"
    )


def test_default_registry_root_uses_live_data_repo(monkeypatch) -> None:
    monkeypatch.delenv("REGISTRAR_REGISTRY_ROOT", raising=False)

    assert (
        default_registry_root() == (Path.home() / ".registrar" / "registry").resolve()
    )


def test_default_registry_root_can_be_overridden(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = tmp_path / "registry"
    monkeypatch.setenv("REGISTRAR_REGISTRY_ROOT", str(registry))

    assert default_registry_root() == registry.resolve()
