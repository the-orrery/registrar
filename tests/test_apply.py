import json
from pathlib import Path

import pytest

from registrar.apply import apply_relocate
from registrar.errors import RegistrarError
from registrar.refs import classify_ref
from registrar.registry import load_registry


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


def test_classify_ref_splits_functional_and_preserved(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert classify_ref(tmp_path / "control-plane" / "assets.json", "text") == "rewrite"
    assert classify_ref(tmp_path / "host-config" / "manifest.toml", "text") == "rewrite"
    assert classify_ref(tmp_path / "dotfiles" / "env.zsh", "text") == "rewrite"
    assert classify_ref(tmp_path / ".local" / "bin" / "foo-run", "symlink") == "rewrite"
    # preserved: docs, runtime records, archives
    assert classify_ref(tmp_path / "host-config" / "catalog.md", "text") == "preserve"
    assert (
        classify_ref(tmp_path / "ops" / "runtime" / "state.json", "text") == "preserve"
    )
    assert classify_ref(tmp_path / "ops" / "audits" / "a.json", "text") == "preserve"
    archived = tmp_path / "workspace-archive" / "old" / "config.toml"
    assert classify_ref(archived, "text") == "preserve"


def test_apply_moves_rewrites_functional_preserves_historical(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(home))
    workspace = (home / "workspace").resolve()
    source = (workspace / "foo").resolve()
    source.mkdir(parents=True)
    (source / "wrapper").write_text("#!/bin/sh\n", encoding="utf-8")

    registry = (tmp_path / "registry").resolve()
    _write_registry(registry, "foo", source, "workspace/projects/team")

    ops = workspace / "control-plane"
    ops.mkdir(parents=True)
    cfg = ops / "assets.json"
    cfg.write_text(json.dumps({"path": str(source)}) + "\n", encoding="utf-8")
    doc = ops / "catalog.md"
    doc.write_text(f"2026-06-01 created at {source}\n", encoding="utf-8")
    runtime = ops / "runtime"
    runtime.mkdir()
    record_json = runtime / "state.json"
    record_json.write_text(json.dumps({"old": str(source)}) + "\n", encoding="utf-8")

    bindir = home / ".local" / "bin"
    bindir.mkdir(parents=True)
    shim = bindir / "foo-run"
    shim.symlink_to(source / "wrapper")

    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    plist = agents / "com.test.foo.plist"
    plist.write_text(f"  <string>{source}/wrapper</string>\n", encoding="utf-8")

    monkeypatch.setenv("REGISTRAR_REF_SCAN_ROOTS", f"{agents}:{bindir}:{ops}")

    records = load_registry(registry)
    result = apply_relocate(records, "foo", workspace, registry)

    target = (workspace / "projects" / "team" / "foo").resolve()
    assert result.moved
    assert target.exists()
    assert not source.exists()

    # functional refs rewritten to the new path
    assert json.loads(cfg.read_text())["path"] == str(target)
    assert shim.readlink() == target / "wrapper"
    assert str(target) in plist.read_text()
    assert str(source) not in plist.read_text()

    # historical / generated refs preserved verbatim
    assert str(source) in doc.read_text()
    assert str(source) in record_json.read_text()
    preserved_sources = {ref.source for ref in result.preserved_refs}
    assert doc in preserved_sources
    assert record_json in preserved_sources

    # registry record now points at the target
    assert str(target) in (registry / "foo.yaml").read_text()

    assert result.verified
    assert result.remaining_refs == ()
    assert str(plist) in result.launchd_reload


def test_apply_refuses_when_target_exists(tmp_path, monkeypatch) -> None:
    home = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("REGISTRAR_REF_SCAN_ROOTS", str(tmp_path / "empty"))
    workspace = (home / "workspace").resolve()
    source = (workspace / "foo").resolve()
    source.mkdir(parents=True)
    blocker = workspace / "projects" / "team" / "foo"
    blocker.mkdir(parents=True)

    registry = (tmp_path / "registry").resolve()
    _write_registry(registry, "foo", source, "workspace/projects/team")
    records = load_registry(registry)

    with pytest.raises(RegistrarError, match="target already exists"):
        apply_relocate(records, "foo", workspace, registry)
    assert source.exists()


def test_apply_refuses_when_already_in_place(tmp_path, monkeypatch) -> None:
    home = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("REGISTRAR_REF_SCAN_ROOTS", str(tmp_path / "empty"))
    workspace = (home / "workspace").resolve()
    source = (workspace / "projects" / "team" / "foo").resolve()
    source.mkdir(parents=True)

    registry = (tmp_path / "registry").resolve()
    _write_registry(registry, "foo", source, "workspace/projects/team")
    records = load_registry(registry)

    with pytest.raises(RegistrarError, match="already at target placement"):
        apply_relocate(records, "foo", workspace, registry)


def test_apply_does_not_touch_sibling_with_shared_prefix(tmp_path, monkeypatch) -> None:
    home = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(home))
    workspace = (home / "workspace").resolve()
    source = (workspace / "foo").resolve()
    source.mkdir(parents=True)
    sibling = (workspace / "foobar").resolve()
    sibling.mkdir(parents=True)

    registry = (tmp_path / "registry").resolve()
    _write_registry(registry, "foo", source, "workspace/projects/team")

    ops = workspace / "control-plane"
    ops.mkdir(parents=True)
    cfg = ops / "config.toml"
    cfg.write_text(f'a = "{source}"\nb = "{sibling}/bin"\n', encoding="utf-8")
    monkeypatch.setenv("REGISTRAR_REF_SCAN_ROOTS", str(ops))

    records = load_registry(registry)
    result = apply_relocate(records, "foo", workspace, registry)

    target = (workspace / "projects" / "team" / "foo").resolve()
    text = cfg.read_text()
    assert f'a = "{target}"' in text  # the moved repo's ref was rewritten
    assert f'b = "{sibling}/bin"' in text  # the prefix-sharing sibling untouched
    assert result.verified


def test_apply_updates_selected_identity_not_same_name_capability(
    tmp_path,
    monkeypatch,
) -> None:
    home = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("REGISTRAR_REF_SCAN_ROOTS", str(tmp_path / "empty"))
    workspace = (home / "workspace").resolve()
    source = (workspace / "foo").resolve()
    source.mkdir(parents=True)

    registry = (tmp_path / "registry").resolve()
    registry.mkdir()
    capability = registry / "a-capability.yaml"
    capability.write_text(
        """
apiVersion: registrar.local/v1alpha1
kind: Capability
metadata:
  identity: capabilities/cli/foo
  name: foo
spec:
  owner_ref: TASK-542
  lifecycle: active
  restore_policy: source-of-truth
  capability_type: cli
  exposures:
    - type: cli
      name: foo
""",
        encoding="utf-8",
    )
    repo = registry / "z-repo.yaml"
    repo.write_text(
        f"""
apiVersion: registrar.local/v1alpha1
kind: Repo
metadata:
  identity: projects/team/foo
  name: foo
  path: {source}
spec:
  owner_ref: TASK-552
  lifecycle: active
  placement: workspace/projects/team
  restore_policy: source-of-truth
""",
        encoding="utf-8",
    )

    records = load_registry(registry)
    result = apply_relocate(records, "projects/team/foo", workspace, registry)

    target = (workspace / "projects" / "team" / "foo").resolve()
    assert result.identity == "projects/team/foo"
    assert str(target) in repo.read_text()
    assert "path:" not in capability.read_text()
    assert result.verified


def test_apply_rewrites_deeper_path_inside_repo(tmp_path, monkeypatch) -> None:
    home = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(home))
    workspace = (home / "workspace").resolve()
    source = (workspace / "foo").resolve()
    source.mkdir(parents=True)

    registry = (tmp_path / "registry").resolve()
    _write_registry(registry, "foo", source, "workspace/projects/team")

    ops = workspace / "control-plane"
    ops.mkdir(parents=True)
    cfg = ops / "checks.toml"
    cfg.write_text(f'cmd = "{source}/scripts/run.sh"\n', encoding="utf-8")
    monkeypatch.setenv("REGISTRAR_REF_SCAN_ROOTS", str(ops))

    records = load_registry(registry)
    apply_relocate(records, "foo", workspace, registry)

    target = (workspace / "projects" / "team" / "foo").resolve()
    assert f'cmd = "{target}/scripts/run.sh"' in cfg.read_text()


def test_apply_rolls_back_everything_on_failure(tmp_path, monkeypatch) -> None:
    home = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(home))
    workspace = (home / "workspace").resolve()
    source = (workspace / "foo").resolve()
    source.mkdir(parents=True)
    (source / "wrapper").write_text("x\n", encoding="utf-8")

    registry = (tmp_path / "registry").resolve()
    _write_registry(registry, "foo", source, "workspace/projects/team")

    ops = workspace / "control-plane"
    ops.mkdir(parents=True)
    cfg = ops / "config.toml"
    original_cfg = f'path = "{source}"\n'
    cfg.write_text(original_cfg, encoding="utf-8")
    bindir = home / ".local" / "bin"
    bindir.mkdir(parents=True)
    shim = bindir / "foo-run"
    shim.symlink_to(source / "wrapper")
    monkeypatch.setenv("REGISTRAR_REF_SCAN_ROOTS", f"{ops}:{bindir}")

    import registrar.apply as apply_mod

    def boom(*_args, **_kwargs):
        raise OSError("simulated move failure")

    monkeypatch.setattr(apply_mod, "_move", boom)

    records = load_registry(registry)
    with pytest.raises(RegistrarError, match="rolled back"):
        apply_relocate(records, "foo", workspace, registry)

    # the move never landed and every reversible edit was restored
    assert source.exists()
    assert not (workspace / "projects" / "team" / "foo").exists()
    assert cfg.read_text() == original_cfg
    assert shim.readlink() == source / "wrapper"
    assert str(source) in (registry / "foo.yaml").read_text()
    assert (
        str((workspace / "sources").resolve())
        not in (registry / "foo.yaml").read_text()
    )
