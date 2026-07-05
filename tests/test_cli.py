import json
from pathlib import Path

from typer.testing import CliRunner

from registrar.cli import app
from registrar.errors import RegistrarError


def _write_registry(registry: Path, name: str, source: Path, placement: str) -> None:
    registry.mkdir(parents=True, exist_ok=True)
    (registry / f"{name}.yaml").write_text(
        f"""
apiVersion: registrar.local/v1alpha1
kind: Repo
metadata:
  name: {name}
  path: {source}
spec:
  owner_ref: TASK-1
  lifecycle: active
  placement: {placement}
  restore_policy: source-of-truth
""",
        encoding="utf-8",
    )


def _write_capability(registry: Path, name: str) -> None:
    registry.mkdir(parents=True, exist_ok=True)
    (registry / f"capability-{name}.yaml").write_text(
        f"""
apiVersion: registrar.local/v1alpha1
kind: Capability
metadata:
  name: {name}
spec:
  owner_ref: TASK-542
  lifecycle: active
  restore_policy: source-of-truth
  capability_type: cli
  exposures:
    - type: cli
      name: {name}
      target: ${{HOME}}/.local/bin/{name}
      state: active
      policy: preferred
""",
        encoding="utf-8",
    )


def test_inventory_cli_prints_table(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "open-source" / "foo").mkdir(parents=True)
    registry = tmp_path / "registry"
    registry.mkdir()
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "inventory",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
        ],
    )

    assert result.exit_code == 0
    assert "foo" in result.stdout
    assert "workspace/open-source" in result.stdout


def test_capabilities_cli_prints_table(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    _write_capability(registry, "hostdiag")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "capabilities",
            "--registry-root",
            str(registry),
        ],
    )

    assert result.exit_code == 0
    assert "hostdiag" in result.stdout
    assert "cli:hostdiag" in result.stdout
    assert "preferred" not in result.stdout


def test_capabilities_cli_prints_json(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    _write_capability(registry, "proxy-route")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "capabilities",
            "--registry-root",
            str(registry),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["items"][0]["kind"] == "Capability"
    assert payload["items"][0]["metadata"] == {
        "identity": "capabilities/cli/proxy-route",
        "labels": {},
        "name": "proxy-route",
    }


def test_relocate_requires_dry_run(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    registry.mkdir()
    runner = CliRunner()

    result = runner.invoke(app, ["relocate", "foo", "--registry-root", str(registry)])

    assert result.exit_code == 1
    assert isinstance(result.exception, RegistrarError)
    assert "requires --dry-run" in result.exception.message


def test_relocate_cli_rejects_capability(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry"
    _write_capability(registry, "hostdiag")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "relocate",
            "hostdiag",
            "--dry-run",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
        ],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, RegistrarError)
    assert "not a filesystem relocation target" in result.exception.message


def test_relocate_short_name_is_ambiguous_when_capability_shares_repo_name(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "projects" / "tools" / "proxy-route"
    source.mkdir(parents=True)
    registry = tmp_path / "registry"
    _write_registry(
        registry,
        "proxy-route",
        source,
        "workspace/projects/tools",
    )
    _write_capability(registry, "proxy-route")
    runner = CliRunner()

    ambiguous = runner.invoke(
        app,
        [
            "relocate",
            "proxy-route",
            "--dry-run",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
        ],
    )
    exact = runner.invoke(
        app,
        [
            "relocate",
            "projects/tools/proxy-route",
            "--dry-run",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
            "--format",
            "json",
        ],
    )

    assert ambiguous.exit_code == 1
    assert isinstance(ambiguous.exception, RegistrarError)
    assert "ambiguous metadata.name" in ambiguous.exception.message
    assert exact.exit_code == 0
    assert json.loads(exact.stdout)["identity"] == "projects/tools/proxy-route"


def test_global_registry_root_option_applies_to_subcommands(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "foo"
    source.mkdir(parents=True)
    registry = tmp_path / "registry"
    _write_registry(registry, "foo", source, "workspace/projects/tools")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "--registry-root",
            str(registry),
            "relocate",
            "foo",
            "--dry-run",
            "--workspace-root",
            str(workspace),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["source_path"] == str(source)


def test_relocate_broad_sweep_reports_workspace_refs_in_json(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    source = workspace / "foo"
    source.mkdir(parents=True)
    registry = workspace / "data" / "registry"
    _write_registry(registry, "foo", source, "workspace/projects/tools")

    sibling = workspace / "other-tool"
    sibling.mkdir(parents=True)
    sibling_ref = sibling / "config.toml"
    sibling_ref.write_text(f'path = "{source}"\n', encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "relocate",
            "foo",
            "--dry-run",
            "--broad-sweep",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert [ref["source"] for ref in payload["workspace_sweep_refs"]] == [
        str(sibling_ref)
    ]


def test_relocate_rejects_apply_with_broad_sweep(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    registry.mkdir()
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "relocate",
            "foo",
            "--apply",
            "--broad-sweep",
            "--registry-root",
            str(registry),
        ],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, RegistrarError)
    assert "--broad-sweep is dry-run only" in result.exception.message
