import json
import os
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from registrar.cli import app
from registrar.errors import RegistrarError
from registrar.registry import load_registry
from registrar.worktree_lifecycle import _reconciliation_candidates


def test_worktree_create_dry_run_prints_plan_without_writing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    registry = tmp_path / "registry"
    registry.mkdir()
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "create",
            str(source),
            "--owner-ref",
            "TASK-542",
            "--world",
            "personal",
            "--slug",
            "worktree-cli",
            "--dry-run",
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
    assert payload["applied"] is False
    assert payload["world"] == "personal"
    assert payload["branch"] == "task-542-worktree-cli"
    assert payload["worktree_path"].endswith("workspace/worktrees/registrar-task-542")
    assert not (workspace / "worktrees" / "registrar-task-542").exists()
    assert not (registry / "assets").exists()


def test_worktree_create_resolves_owner_uid_with_docket(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    registry = tmp_path / "registry"
    registry.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docket = bin_dir / "docket"
    docket.write_text(
        "#!/bin/sh\n"
        "cat <<'JSON'\n"
        '{"uid":"dkt_0123456789abcdef0123456789abcdef",'
        '"id":"ERI-908","display_ref":"WORK-12",'
        '"status":"In Progress","state_type":"started"}\n'
        "JSON\n",
        encoding="utf-8",
    )
    docket.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "create",
            str(source),
            "--owner-ref",
            "ERI-908",
            "--world",
            "personal",
            "--dry-run",
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
    assert payload["owner_ref"] == "WORK-12"
    assert payload["owner_uid"] == "dkt_0123456789abcdef0123456789abcdef"
    assert payload["worktree_path"].endswith("workspace/worktrees/registrar-work-12")
    assert (
        payload["document"]["metadata"]["labels"]["issue_uid"] == payload["owner_uid"]
    )
    assert payload["document"]["spec"]["owner_uid"] == payload["owner_uid"]


def test_worktree_create_creates_git_worktree_and_registry_record(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    registry = tmp_path / "registry"
    registry.mkdir()
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "create",
            str(source),
            "--owner-ref",
            "TASK-542",
            "--world",
            "personal",
            "--slug",
            "worktree-cli",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
            "--format",
            "json",
        ],
    )

    worktree = workspace / "worktrees" / "registrar-task-542"
    record = registry / "assets" / "worktree-registrar-task-542.yaml"
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["applied"] is True
    assert worktree.exists()
    assert record.exists()
    assert "owner_ref: TASK-542" in record.read_text(encoding="utf-8")
    assert "world: personal" in record.read_text(encoding="utf-8")


def test_worktree_create_uses_slug_for_path_only_when_owner_path_exists(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    (workspace / "worktrees" / "registrar-task-542").mkdir(parents=True)
    registry = tmp_path / "registry"
    registry.mkdir()
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "create",
            str(source),
            "--owner-ref",
            "TASK-542",
            "--world",
            "personal",
            "--slug",
            "worktree-cli",
            "--dry-run",
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
    assert payload["worktree_path"].endswith(
        "workspace/worktrees/registrar-task-542-worktree-cli"
    )


def test_worktree_create_rejects_unowned_without_breakglass(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    registry = tmp_path / "registry"
    registry.mkdir()
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "create",
            str(source),
            "--owner-ref",
            "none:temporary",
            "--world",
            "personal",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
        ],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, RegistrarError)
    assert "create or reuse a docket issue" in result.exception.message
    assert "--allow-unowned" in result.exception.message


def test_worktree_create_allows_unowned_with_breakglass(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    registry = tmp_path / "registry"
    registry.mkdir()
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "create",
            str(source),
            "--owner-ref",
            "none:temporary",
            "--allow-unowned",
            "--world",
            "personal",
            "--slug",
            "scratch",
            "--dry-run",
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
    assert payload["owner_ref"] == "none:temporary"
    assert payload["branch"] == "none-temporary-scratch"


def test_worktree_register_writes_existing_worktree_record(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "services" / "collector")
    worktree = workspace / "worktrees" / "collector-team-588"
    _run(
        "git",
        "-C",
        str(source),
        "worktree",
        "add",
        "-b",
        "team-588",
        str(worktree),
        "HEAD",
    )
    registry = tmp_path / "registry"
    registry.mkdir()
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "register",
            str(worktree),
            "--owner-ref",
            "TEAM-588",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
            "--format",
            "json",
        ],
    )

    record = registry / "assets" / "worktree-collector-team-588.yaml"
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["applied"] is True
    assert payload["world"] == "work"
    assert payload["source_repo"] == "collector"
    assert record.exists()
    assert "issue: TEAM-588" in record.read_text(encoding="utf-8")


def test_worktree_migrate_owners_backfills_owner_uid(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    registry = tmp_path / "registry"
    record = _write_worktree_record(
        registry,
        workspace / "worktrees" / "registrar-task-542",
        "TASK-542",
    )
    _fake_docket_resolve(
        tmp_path,
        monkeypatch,
        display_ref="WORK-12",
        uid="dkt_0123456789abcdef0123456789abcdef",
    )
    runner = CliRunner()

    dry_run = runner.invoke(
        app,
        [
            "worktree",
            "migrate-owners",
            "--dry-run",
            "--registry-root",
            str(registry),
            "--format",
            "json",
        ],
    )
    result = runner.invoke(
        app,
        [
            "worktree",
            "migrate-owners",
            "--registry-root",
            str(registry),
            "--format",
            "json",
        ],
    )

    assert dry_run.exit_code == 0
    [dry_item] = json.loads(dry_run.stdout)
    assert dry_item["changed"] is True
    assert dry_item["applied"] is False
    assert result.exit_code == 0
    [item] = json.loads(result.stdout)
    assert item["before_owner_ref"] == "TASK-542"
    assert item["after_owner_ref"] == "WORK-12"
    assert item["after_owner_uid"] == "dkt_0123456789abcdef0123456789abcdef"
    assert item["applied"] is True
    text = record.read_text(encoding="utf-8")
    assert "owner_ref: WORK-12" in text
    assert "owner_uid: dkt_0123456789abcdef0123456789abcdef" in text
    assert "issue: WORK-12" in text
    assert "issue_uid: dkt_0123456789abcdef0123456789abcdef" in text


def test_worktree_register_rejects_unowned_without_breakglass(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "services" / "collector")
    worktree = workspace / "worktrees" / "collector-scratch"
    _run(
        "git",
        "-C",
        str(source),
        "worktree",
        "add",
        "-b",
        "scratch",
        str(worktree),
        "HEAD",
    )
    registry = tmp_path / "registry"
    registry.mkdir()
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "register",
            str(worktree),
            "--owner-ref",
            "none:temporary",
            "--world",
            "personal",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
        ],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, RegistrarError)
    assert "create or reuse a docket issue" in result.exception.message


def test_worktree_register_rejects_non_worktree_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    registry = tmp_path / "registry"
    registry.mkdir()
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "register",
            str(source),
            "--owner-ref",
            "TASK-542",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
        ],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, RegistrarError)
    assert "worktree path must be under" in result.exception.message


def test_worktree_register_rejects_tombstone_identity_conflict(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    worktree = workspace / "worktrees" / "registrar-task-542"
    _run(
        "git",
        "-C",
        str(source),
        "worktree",
        "add",
        "-b",
        "task-542",
        str(worktree),
        "HEAD",
    )
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "worktree-registrar-task-542.yaml").write_text(
        """
apiVersion: registrar.local/v1alpha1
kind: Tombstone
metadata:
  identity: worktrees/registrar-task-542
  name: registrar-task-542
  labels:
    old_path: /tmp/registrar-task-542
spec:
  owner_ref: TASK-542
  lifecycle: removed
  placement: workspace/worktrees
  restore_policy: tombstone
""",
        encoding="utf-8",
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "register",
            str(worktree),
            "--owner-ref",
            "TASK-542",
            "--world",
            "personal",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
        ],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, RegistrarError)
    assert "registry identity already exists tombstone" in result.exception.message


def test_doctor_points_unowned_worktrees_to_register_command(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    worktree = workspace / "worktrees" / "registrar-task-542"
    _run(
        "git",
        "-C",
        str(source),
        "worktree",
        "add",
        "-b",
        "task-542",
        str(worktree),
        "HEAD",
    )
    registry = tmp_path / "registry"
    registry.mkdir()
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "doctor",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    findings = json.loads(result.stdout)
    worktree_finding = next(
        item for item in findings if item["name"] == "registrar-task-542"
    )
    assert "registrar worktree register" in worktree_finding["next_action"]
    assert "<ISSUE-REF>" in worktree_finding["next_action"]
    assert "none:" not in worktree_finding["next_action"]


def test_worktree_audit_reports_registered_worktree_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    worktree = workspace / "worktrees" / "registrar-task-542"
    _run(
        "git",
        "-C",
        str(source),
        "worktree",
        "add",
        "-b",
        "task-542",
        str(worktree),
        "HEAD",
    )
    registry = tmp_path / "registry"
    _write_worktree_record(registry, worktree, "TASK-542")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "audit",
            "--owner-ref",
            "TASK-542",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    [item] = json.loads(result.stdout)
    assert item["name"] == "registrar-task-542"
    assert item["path_state"] == "exists"
    assert item["branch"] == "task-542"
    assert item["branch_state"] == "merged"
    assert item["owner_state"] in {
        "unknown",
        "started",
        "unstarted",
        "completed",
        "canceled",
    }


def test_worktree_list_alias_reports_registered_worktree_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    worktree = workspace / "worktrees" / "registrar-task-542"
    _run(
        "git",
        "-C",
        str(source),
        "worktree",
        "add",
        "-b",
        "task-542",
        str(worktree),
        "HEAD",
    )
    registry = tmp_path / "registry"
    _write_worktree_record(registry, worktree, "TASK-542")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "list",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    [item] = json.loads(result.stdout)
    assert item["name"] == "registrar-task-542"
    assert item["owner_ref"] == "TASK-542"


def test_worktree_owner_reports_registered_owner_for_nested_path(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    worktree = workspace / "worktrees" / "registrar-task-542"
    _run(
        "git",
        "-C",
        str(source),
        "worktree",
        "add",
        "-b",
        "task-542",
        str(worktree),
        "HEAD",
    )
    nested = worktree / "docs"
    nested.mkdir()
    registry = tmp_path / "registry"
    _write_worktree_record(registry, worktree, "TASK-542")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "owner",
            str(nested),
            "--registry-root",
            str(registry),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["found"] is True
    assert payload["owner_ref"] == "TASK-542"
    assert payload["record_path"] == str(worktree.resolve())


def test_worktree_owner_reports_missing_registration(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    worktree = workspace / "worktrees" / "registrar-task-542"
    _run(
        "git",
        "-C",
        str(source),
        "worktree",
        "add",
        "-b",
        "task-542",
        str(worktree),
        "HEAD",
    )
    registry = tmp_path / "registry"
    registry.mkdir()
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "owner",
            str(worktree),
            "--registry-root",
            str(registry),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["found"] is False
    assert payload["owner_ref"] == ""


def test_worktree_audit_counts_untracked_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    worktree = workspace / "worktrees" / "registrar-task-542"
    _run(
        "git",
        "-C",
        str(source),
        "worktree",
        "add",
        "-b",
        "task-542",
        str(worktree),
        "HEAD",
    )
    (worktree / "scratch.txt").write_text("scratch\n", encoding="utf-8")
    registry = tmp_path / "registry"
    _write_worktree_record(registry, worktree, "TASK-542")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "audit",
            "registrar-task-542",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    [item] = json.loads(result.stdout)
    assert item["untracked_count"] == 1
    assert item["recommendation"] == "blocked: untracked"
    assert item["close_gate_state"] == "untracked"


def test_worktree_reconcile_blocks_owner_close_until_worktree_closed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    worktree = workspace / "worktrees" / "registrar-task-542"
    _run(
        "git",
        "-C",
        str(source),
        "worktree",
        "add",
        "-b",
        "task-542",
        str(worktree),
        "HEAD",
    )
    registry = tmp_path / "registry"
    _write_worktree_record(registry, worktree, "TASK-542")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "reconcile",
            "TASK-542",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, RegistrarError)
    payload = json.loads(result.stdout)
    assert payload["blocked"] is True
    assert payload["active_count"] == 1
    assert payload["ready_to_close_count"] == 1
    [item] = payload["items"]
    assert item["name"] == "registrar-task-542"
    assert item["close_gate_state"] == "ready-to-close"
    assert "registrar worktree closeout registrar-task-542" in item["close_gate_action"]


def test_worktree_reconcile_accepts_owner_alias(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    worktree = workspace / "worktrees" / "registrar-task-542"
    _run(
        "git",
        "-C",
        str(source),
        "worktree",
        "add",
        "-b",
        "task-542",
        str(worktree),
        "HEAD",
    )
    registry = tmp_path / "registry"
    _write_worktree_record(registry, worktree, "TASK-542")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "reconcile",
            "ISSUE-542",
            "--alias",
            "TASK-542",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["owner_refs"] == ["ISSUE-542", "TASK-542"]
    assert payload["active_count"] == 1


def test_worktree_reconcile_allows_owner_without_worktrees(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    registry = tmp_path / "registry"
    registry.mkdir()
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "reconcile",
            "TASK-542",
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
    assert payload["blocked"] is False
    assert payload["active_count"] == 0


def test_reconcile_candidates_only_audit_uid_matched_records(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    registry = tmp_path / "registry"
    _write_worktree_record(
        registry,
        workspace / "worktrees" / "target",
        "OPS-8",
        owner_uid="dkt_target",
        name="target",
    )
    _write_worktree_record(
        registry,
        workspace / "worktrees" / "unrelated",
        "OPS-7",
        owner_uid="dkt_unrelated",
        name="unrelated",
    )

    candidates = _reconciliation_candidates(
        load_registry(registry),
        workspace,
        ("dkt_target", "OPS-8"),
        include_retired=False,
    )

    assert [record.name for record in candidates] == ["target"]


def test_reconcile_candidates_keep_legacy_records_on_safe_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    registry = tmp_path / "registry"
    _write_worktree_record(
        registry,
        workspace / "worktrees" / "target",
        "OPS-8",
        owner_uid="dkt_target",
        name="target",
    )
    _write_worktree_record(
        registry,
        workspace / "worktrees" / "legacy",
        "HISTORICAL-8",
        name="legacy",
    )

    candidates = _reconciliation_candidates(
        load_registry(registry),
        workspace,
        ("dkt_target", "OPS-8"),
        include_retired=False,
    )

    assert [record.name for record in candidates] == ["legacy", "target"]


def test_worktree_closeout_apply_removes_worktree_and_active_record(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    worktree = workspace / "worktrees" / "registrar-task-542"
    _run(
        "git",
        "-C",
        str(source),
        "worktree",
        "add",
        "-b",
        "task-542",
        str(worktree),
        "HEAD",
    )
    registry = tmp_path / "registry"
    record = _write_worktree_record(registry, worktree, "TASK-542")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "closeout",
            "registrar-task-542",
            "--apply",
            "--owner-active-ok",
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
    assert payload["applied"] is True
    assert not worktree.exists()
    assert not record.exists()
    branch_check = subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/task-542",
        ],
        check=False,
    )
    assert branch_check.returncode == 0


def test_worktree_closeout_blocks_dirty_worktree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    worktree = workspace / "worktrees" / "registrar-task-542"
    _run(
        "git",
        "-C",
        str(source),
        "worktree",
        "add",
        "-b",
        "task-542",
        str(worktree),
        "HEAD",
    )
    (worktree / "README.md").write_text("# changed\n", encoding="utf-8")
    registry = tmp_path / "registry"
    _write_worktree_record(registry, worktree, "TASK-542")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "closeout",
            "registrar-task-542",
            "--apply",
            "--owner-active-ok",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
        ],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, RegistrarError)
    assert "dirty" in result.exception.message
    assert worktree.exists()


def test_worktree_closeout_stale_record_requires_explicit_flag(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    missing = workspace / "worktrees" / "registrar-task-542"
    registry = tmp_path / "registry"
    record = _write_worktree_record(registry, missing, "TASK-542")
    runner = CliRunner()

    blocked = runner.invoke(
        app,
        [
            "worktree",
            "closeout",
            "registrar-task-542",
            "--apply",
            "--owner-active-ok",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
        ],
    )
    applied = runner.invoke(
        app,
        [
            "worktree",
            "closeout",
            "registrar-task-542",
            "--apply",
            "--stale-record",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
        ],
    )

    assert blocked.exit_code == 1
    assert isinstance(blocked.exception, RegistrarError)
    assert "stale-record-required" in blocked.exception.message
    assert applied.exit_code == 0
    assert not record.exists()


def test_worktree_closeout_force_removes_dirty_worktree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    worktree = workspace / "worktrees" / "registrar-task-542"
    _run(
        "git",
        "-C",
        str(source),
        "worktree",
        "add",
        "-b",
        "task-542",
        str(worktree),
        "HEAD",
    )
    (worktree / "README.md").write_text("# changed\n", encoding="utf-8")
    registry = tmp_path / "registry"
    record = _write_worktree_record(registry, worktree, "TASK-542")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "closeout",
            "registrar-task-542",
            "--apply",
            "--owner-active-ok",
            "--force",
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
    assert payload["applied"] is True
    assert not worktree.exists()
    assert not record.exists()
    branch_check = subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/task-542",
        ],
        check=False,
    )
    assert branch_check.returncode == 0


def test_worktree_closeout_delete_branch_drops_local_branch(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    worktree = workspace / "worktrees" / "registrar-task-542"
    _run(
        "git",
        "-C",
        str(source),
        "worktree",
        "add",
        "-b",
        "task-542",
        str(worktree),
        "HEAD",
    )
    registry = tmp_path / "registry"
    record = _write_worktree_record(registry, worktree, "TASK-542")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "closeout",
            "registrar-task-542",
            "--apply",
            "--owner-active-ok",
            "--delete-branch",
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
    assert payload["applied"] is True
    assert not worktree.exists()
    assert not record.exists()
    branch_check = subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/task-542",
        ],
        check=False,
    )
    assert branch_check.returncode != 0


def test_worktree_remove_alias_closes_out_like_closeout(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    worktree = workspace / "worktrees" / "registrar-task-542"
    _run(
        "git",
        "-C",
        str(source),
        "worktree",
        "add",
        "-b",
        "task-542",
        str(worktree),
        "HEAD",
    )
    registry = tmp_path / "registry"
    record = _write_worktree_record(registry, worktree, "TASK-542")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "remove",
            "registrar-task-542",
            "--apply",
            "--owner-active-ok",
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
    assert payload["applied"] is True
    assert not worktree.exists()
    assert not record.exists()


def test_worktree_create_auto_commits_record_when_registry_is_git_repo(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    registry = _git_init(tmp_path / "registry")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "create",
            str(source),
            "--owner-ref",
            "TASK-542",
            "--world",
            "personal",
            "--slug",
            "worktree-cli",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
            "--format",
            "json",
        ],
    )

    record = registry / "assets" / "worktree-registrar-task-542.yaml"
    assert result.exit_code == 0
    assert record.exists()
    tracked = subprocess.run(
        ["git", "-C", str(registry), "ls-files", "--", str(record)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert tracked.stdout.strip() != ""
    status = subprocess.run(
        ["git", "-C", str(registry), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout.strip() == ""
    log = subprocess.run(
        ["git", "-C", str(registry), "log", "-1", "--format=%s"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "register worktree" in log.stdout


def test_worktree_migrate_owners_auto_commits_registry_record(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    registry = _git_init(tmp_path / "registry")
    _write_worktree_record(
        registry,
        workspace / "worktrees" / "registrar-task-542",
        "TASK-542",
    )
    _run("git", "-C", str(registry), "add", "-A")
    _run("git", "-C", str(registry), "commit", "-q", "-m", "seed record")
    _fake_docket_resolve(
        tmp_path,
        monkeypatch,
        display_ref="WORK-12",
        uid="dkt_0123456789abcdef0123456789abcdef",
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "migrate-owners",
            "--registry-root",
            str(registry),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    status = subprocess.run(
        ["git", "-C", str(registry), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout.strip() == ""
    log = subprocess.run(
        ["git", "-C", str(registry), "log", "-1", "--format=%s"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "migrate worktree owner uid" in log.stdout


def test_worktree_closeout_auto_commits_record_deletion(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _git_repo(workspace / "projects" / "tools" / "registrar")
    worktree = workspace / "worktrees" / "registrar-task-542"
    _run(
        "git",
        "-C",
        str(source),
        "worktree",
        "add",
        "-b",
        "task-542",
        str(worktree),
        "HEAD",
    )
    registry = _git_init(tmp_path / "registry")
    record = _write_worktree_record(registry, worktree, "TASK-542")
    _run("git", "-C", str(registry), "add", "-A")
    _run("git", "-C", str(registry), "commit", "-q", "-m", "seed record")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "worktree",
            "closeout",
            "registrar-task-542",
            "--apply",
            "--owner-active-ok",
            "--workspace-root",
            str(workspace),
            "--registry-root",
            str(registry),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert not record.exists()
    status = subprocess.run(
        ["git", "-C", str(registry), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout.strip() == ""
    log = subprocess.run(
        ["git", "-C", str(registry), "log", "-1", "--format=%s"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "closeout worktree" in log.stdout


def _write_worktree_record(
    registry: Path,
    worktree: Path,
    owner_ref: str,
    *,
    owner_uid: str = "",
    name: str = "registrar-task-542",
) -> Path:
    registry.mkdir(parents=True, exist_ok=True)
    record = registry / f"worktree-{name}.yaml"
    owner_uid_line = f"  owner_uid: {owner_uid}\n" if owner_uid else ""
    record.write_text(
        f"""
apiVersion: registrar.local/v1alpha1
kind: Worktree
metadata:
  identity: worktrees/{name}
  name: {name}
  path: {worktree}
  labels:
    world: personal
    source_repo: registrar
    role: linked-worktree
    issue: {owner_ref}
spec:
  owner_ref: {owner_ref}
{owner_uid_line}  lifecycle: active
  placement: workspace/worktrees
  restore_policy: linked-worktree
  allowed_actions:
    - inspect
    - relocate-dry-run
    - closeout-dry-run
  closeout_policy: require-finalizers
finalizers:
  - pm-owner-required
  - branch-preserved
  - closeout-recorded
  - principal-approval-required
""",
        encoding="utf-8",
    )
    return record


def _fake_docket_resolve(
    tmp_path: Path,
    monkeypatch,
    *,
    display_ref: str,
    uid: str,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docket = bin_dir / "docket"
    docket.write_text(
        "#!/bin/sh\n"
        "cat <<'JSON'\n"
        f'{{"uid":"{uid}","id":"ERI-908","display_ref":"{display_ref}",'
        '"status":"In Progress","state_type":"started"}\n'
        "JSON\n",
        encoding="utf-8",
    )
    docket.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _run("git", "-C", str(path), "init")
    (path / "README.md").write_text("# test\n", encoding="utf-8")
    _run("git", "-C", str(path), "add", "README.md")
    _run(
        "git",
        "-C",
        str(path),
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test User",
        "commit",
        "-m",
        "init",
    )
    return path


def _git_init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run("git", "-C", str(path), "init")
    _run("git", "-C", str(path), "config", "user.email", "registrar-test@example.com")
    _run("git", "-C", str(path), "config", "user.name", "Registrar Test")
    return path


def _run(*args: str) -> None:
    subprocess.run(args, check=True, capture_output=True, text=True)
