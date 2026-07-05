from pathlib import Path

import pytest

from registrar.doctor import run_doctor
from registrar.errors import RegistrarError
from registrar.plan import closeout_plan, relocate_plan
from registrar.registry import by_name_or_path, load_registry


def test_registry_loads_yaml_and_relocate_plan(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "foo"
    source.mkdir(parents=True)
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "foo.yaml").write_text(
        f"""
apiVersion: registrar.local/v1alpha1
kind: Repo
metadata:
  name: foo
  path: {source}
spec:
  owner_ref: TASK-542
  lifecycle: active
  placement: workspace/open-source
  restore_policy: source-of-truth
finalizers:
  - pm-owner-required
""",
        encoding="utf-8",
    )

    records = load_registry(registry)
    plan = relocate_plan(records, "foo", workspace)

    assert records[0].identity == "open-source/foo"
    assert plan.identity == "open-source/foo"
    assert plan.source_path == source
    assert plan.target_path == workspace / "open-source" / "foo"
    assert "placement" in plan.reason


def test_registry_loads_capability_without_path(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "proxy-route.yaml").write_text(
        """
apiVersion: registrar.local/v1alpha1
kind: Capability
metadata:
  name: proxy-route
  labels:
    domain: networking
spec:
  owner_ref: TASK-542
  lifecycle: active
  restore_policy: source-of-truth
  capability_type: cli
  implementation_refs:
    - repo:projects/tools/proxy-route
  exposures:
    - type: cli
      name: proxy-route
      target: ${{HOME}}/.local/bin/proxy-route
      state: active
      policy: preferred
""",
        encoding="utf-8",
    )

    [record] = load_registry(registry)

    assert record.kind == "Capability"
    assert record.identity == "capabilities/cli/proxy-route"
    assert record.path is None
    assert record.spec.capability_type == "cli"
    assert record.spec.exposures[0].name == "proxy-route"
    assert record.to_dict()["metadata"] == {
        "identity": "capabilities/cli/proxy-route",
        "name": "proxy-route",
        "labels": {"domain": "networking"},
    }


def test_registry_loads_tombstone_without_path(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "old-container.yaml").write_text(
        """
apiVersion: registrar.local/v1alpha1
kind: Tombstone
metadata:
  identity: tombstones/archive/old-container
  name: old-container
  labels:
    old_path: /tmp/workspace/old-container
spec:
  owner_ref: TASK-552
  lifecycle: retired
  placement: removed
  restore_policy: none
""",
        encoding="utf-8",
    )

    [record] = load_registry(registry)
    plan = closeout_plan(
        records=[record], asset="old-container", workspace_root=tmp_path
    )
    findings = run_doctor(
        tmp_path / "workspace",
        [record],
        external_root=tmp_path / "external-readonly",
    )

    assert record.kind == "Tombstone"
    assert record.path is None
    assert plan.blocked is False
    assert plan.actions == ("already closed out; tombstone recorded",)
    assert findings == []


def test_registry_rejects_tombstone_with_path(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "old-container.yaml").write_text(
        """
apiVersion: registrar.local/v1alpha1
kind: Tombstone
metadata:
  identity: tombstones/archive/old-container
  name: old-container
  path: /tmp/workspace/old-container
spec:
  owner_ref: TASK-552
  lifecycle: retired
  placement: removed
  restore_policy: none
""",
        encoding="utf-8",
    )

    with pytest.raises(RegistrarError, match="metadata.path is not allowed"):
        load_registry(registry)


def test_registry_rejects_capability_without_exposures(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "bad-capability.yaml").write_text(
        """
apiVersion: registrar.local/v1alpha1
kind: Capability
metadata:
  name: bad-capability
spec:
  owner_ref: TASK-542
  lifecycle: active
  restore_policy: source-of-truth
  capability_type: cli
""",
        encoding="utf-8",
    )

    with pytest.raises(RegistrarError, match="spec.exposures"):
        load_registry(registry)


def test_registry_rejects_capability_with_path(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "bad-capability.yaml").write_text(
        """
apiVersion: registrar.local/v1alpha1
kind: Capability
metadata:
  name: bad-capability
  path: /tmp/fake
spec:
  owner_ref: TASK-542
  lifecycle: active
  restore_policy: source-of-truth
  capability_type: cli
  exposures:
    - type: cli
      name: bad-capability
""",
        encoding="utf-8",
    )

    with pytest.raises(RegistrarError, match="metadata.path is not allowed"):
        load_registry(registry)


def test_registry_allows_duplicate_names_with_distinct_identities(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "proxy-route"
    source.mkdir(parents=True)
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "repo.yaml").write_text(
        f"""
apiVersion: registrar.local/v1alpha1
kind: Repo
metadata:
  identity: projects/tools/proxy-route
  name: proxy-route
  path: {source}
spec:
  owner_ref: TASK-552
  lifecycle: active
  placement: workspace/projects/tools
  restore_policy: source-of-truth
""",
        encoding="utf-8",
    )
    (registry / "capability.yaml").write_text(
        """
apiVersion: registrar.local/v1alpha1
kind: Capability
metadata:
  identity: capabilities/personal/cli/proxy-route
  name: proxy-route
spec:
  owner_ref: TASK-542
  lifecycle: active
  restore_policy: source-of-truth
  capability_type: cli
  exposures:
    - type: cli
      name: proxy-route
""",
        encoding="utf-8",
    )

    records = load_registry(registry)

    assert by_name_or_path(records, "projects/tools/proxy-route").kind == "Repo"
    assert by_name_or_path(records, str(source)).kind == "Repo"
    assert (
        by_name_or_path(records, "capabilities/personal/cli/proxy-route").kind
        == "Capability"
    )
    with pytest.raises(RegistrarError, match="ambiguous metadata.name"):
        by_name_or_path(records, "proxy-route")


def test_registry_aliases_resolve_ambiguous_names_without_changing_paths(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    root = workspace / "platform"
    kb = workspace / "knowledge" / "team" / "platform"
    root.mkdir(parents=True)
    kb.mkdir(parents=True)
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "root.yaml").write_text(
        f"""
apiVersion: registrar.local/v1alpha1
kind: Repo
metadata:
  identity: workspace/root/platform
  name: platform
  aliases:
    - platform-root
  path: {root}
spec:
  owner_ref: TASK-552
  lifecycle: active
  placement: workspace/root
  restore_policy: source-of-truth
""",
        encoding="utf-8",
    )
    (registry / "kb.yaml").write_text(
        f"""
apiVersion: registrar.local/v1alpha1
kind: Repo
metadata:
  identity: knowledge/team/platform
  name: platform
  aliases:
    - platform-kb
  path: {kb}
spec:
  owner_ref: TASK-552
  lifecycle: active
  placement: workspace/knowledge/team
  restore_policy: source-of-truth
""",
        encoding="utf-8",
    )

    records = load_registry(registry)

    assert by_name_or_path(records, "platform-root").identity == (
        "workspace/root/platform"
    )
    assert by_name_or_path(records, "platform-kb").identity == (
        "knowledge/team/platform"
    )
    assert relocate_plan(records, "platform-kb", workspace).target_path == kb
    assert by_name_or_path(records, "platform-kb").to_dict()["metadata"]["aliases"] == [
        "platform-kb"
    ]
    with pytest.raises(RegistrarError, match="ambiguous metadata.name"):
        by_name_or_path(records, "platform")


def test_registry_rejects_duplicate_aliases(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source_a = workspace / "a"
    source_b = workspace / "b"
    source_a.mkdir(parents=True)
    source_b.mkdir(parents=True)
    registry = tmp_path / "registry"
    registry.mkdir()
    for name, source in {"a": source_a, "b": source_b}.items():
        (registry / f"{name}.yaml").write_text(
            f"""
apiVersion: registrar.local/v1alpha1
kind: Repo
metadata:
  identity: projects/team/{name}
  name: {name}
  aliases:
    - shared-short-name
  path: {source}
spec:
  owner_ref: TASK-552
  lifecycle: active
  placement: workspace/projects/team
  restore_policy: source-of-truth
""",
            encoding="utf-8",
        )

    with pytest.raises(RegistrarError, match="duplicate metadata.aliases"):
        load_registry(registry)


def test_registry_rejects_alias_colliding_with_name(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source_a = workspace / "a"
    source_b = workspace / "b"
    source_a.mkdir(parents=True)
    source_b.mkdir(parents=True)
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "a.yaml").write_text(
        f"""
apiVersion: registrar.local/v1alpha1
kind: Repo
metadata:
  identity: projects/team/a
  name: a
  aliases:
    - b
  path: {source_a}
spec:
  owner_ref: TASK-552
  lifecycle: active
  placement: workspace/projects/team
  restore_policy: source-of-truth
""",
        encoding="utf-8",
    )
    (registry / "b.yaml").write_text(
        f"""
apiVersion: registrar.local/v1alpha1
kind: Repo
metadata:
  identity: projects/team/b
  name: b
  path: {source_b}
spec:
  owner_ref: TASK-552
  lifecycle: active
  placement: workspace/projects/team
  restore_policy: source-of-truth
""",
        encoding="utf-8",
    )

    with pytest.raises(RegistrarError, match="collides with metadata.name"):
        load_registry(registry)


def test_registry_rejects_duplicate_identities(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source_a = workspace / "a"
    source_b = workspace / "b"
    source_a.mkdir(parents=True)
    source_b.mkdir(parents=True)
    registry = tmp_path / "registry"
    registry.mkdir()
    for name, source in {"a": source_a, "b": source_b}.items():
        (registry / f"{name}.yaml").write_text(
            f"""
apiVersion: registrar.local/v1alpha1
kind: Repo
metadata:
  identity: projects/tools/same
  name: {name}
  path: {source}
spec:
  owner_ref: TASK-552
  lifecycle: active
  placement: workspace/projects/tools
  restore_policy: source-of-truth
""",
            encoding="utf-8",
        )

    with pytest.raises(RegistrarError, match="duplicate metadata.identity"):
        load_registry(registry)


def test_relocate_rejects_capability_records(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "hostdiag.yaml").write_text(
        """
apiVersion: registrar.local/v1alpha1
kind: Capability
metadata:
  name: hostdiag
spec:
  owner_ref: TASK-542
  lifecycle: active
  restore_policy: source-of-truth
  capability_type: cli
  exposures:
    - type: cli
      name: hostdiag
      state: active
""",
        encoding="utf-8",
    )

    with pytest.raises(RegistrarError, match="not a filesystem relocation target"):
        relocate_plan(load_registry(registry), "hostdiag", workspace)


def test_doctor_reports_missing_owner_and_placement_drift(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "foo"
    source.mkdir(parents=True)
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "foo.yaml").write_text(
        f"""
apiVersion: registrar.local/v1alpha1
kind: Repo
metadata:
  name: foo
  path: {source}
spec:
  owner_ref: ""
  lifecycle: active
  placement: workspace/open-source
  restore_policy: source-of-truth
""",
        encoding="utf-8",
    )

    findings = run_doctor(workspace, load_registry(registry), tmp_path / "external")
    codes = {finding.code for finding in findings}

    assert "no-owner" in codes
    assert "placement-drift" in codes


def test_doctor_does_not_match_external_clone_by_name(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "projects" / "skills" / "agent-skills"
    source.mkdir(parents=True)
    external = tmp_path / "external-readonly"
    external_clone = external / "agent-skills"
    external_clone.mkdir(parents=True)
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "agent-skills.yaml").write_text(
        f"""
apiVersion: registrar.local/v1alpha1
kind: Repo
metadata:
  name: agent-skills
  path: {source}
spec:
  owner_ref: TASK-552
  lifecycle: active
  placement: workspace/projects/skills
  restore_policy: source-of-truth
""",
        encoding="utf-8",
    )

    findings = run_doctor(workspace, load_registry(registry), external)

    assert not any(
        finding.code == "placement-drift" and finding.name == "agent-skills"
        for finding in findings
    )
    assert any(
        finding.code == "no-owner" and finding.path == external_clone
        for finding in findings
    )


def test_doctor_ignores_capability_without_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "agent-entrypoints.yaml").write_text(
        """
apiVersion: registrar.local/v1alpha1
kind: Capability
metadata:
  name: agent-entrypoints
spec:
  owner_ref: TASK-542
  lifecycle: active
  restore_policy: source-of-truth
  capability_type: agent-runtime-bundle
  exposures:
    - type: shell
      name: claude
      state: blocked
      policy: use managed entrypoints
""",
        encoding="utf-8",
    )

    findings = run_doctor(workspace, load_registry(registry), tmp_path / "external")

    assert findings == []


def test_closeout_unregistered_asset_requires_owner(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "scratch").mkdir(parents=True)

    plan = closeout_plan([], "scratch", workspace)

    assert plan.blocked is True
    assert "pm-owner-required" in plan.finalizers


def test_registry_rejects_wrong_api_version(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "bad.yaml").write_text(
        """
apiVersion: wrong/v1
kind: Repo
metadata:
  name: bad
  path: /tmp/bad
spec:
  owner_ref: TASK-542
  lifecycle: active
  placement: workspace/root
  restore_policy: source-of-truth
""",
        encoding="utf-8",
    )

    with pytest.raises(RegistrarError):
        load_registry(registry)
