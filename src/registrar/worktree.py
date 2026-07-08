"""Worktree creation and registry registration helpers."""

from __future__ import annotations

import os
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import RegistrarError
from .git import commit_registry_change
from .model import API_VERSION, TOMBSTONE_KIND, RegistryAsset
from .paths import current_placement
from .registry import derive_identity


def _load_prefix_sets() -> tuple[set[str], set[str]]:
    """Load owner-ref prefix→world mapping from env or defaults."""
    work = os.environ.get("REGISTRAR_WORK_PREFIXES", "")
    personal = os.environ.get("REGISTRAR_PERSONAL_PREFIXES", "")
    return (
        {p.strip() for p in work.split(",") if p.strip()} or {"TEAM", "ORG"},
        {p.strip() for p in personal.split(",") if p.strip()} or {"LAB", "NOTE", "APP"},
    )


WORK_PREFIXES, PERSONAL_PREFIXES = _load_prefix_sets()


@dataclass(frozen=True)
class NormalizedOwner:
    ref: str
    uid: str = ""


@dataclass(frozen=True)
class WorktreePlan:
    action: str
    owner_ref: str
    owner_uid: str
    world: str
    source_repo_path: Path | None
    source_repo: str
    worktree_path: Path
    branch: str
    registry_file: Path
    document: dict[str, Any]
    command: tuple[str, ...] = ()
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "applied": self.applied,
            "owner_ref": self.owner_ref,
            "owner_uid": self.owner_uid,
            "world": self.world,
            "source_repo_path": (
                str(self.source_repo_path) if self.source_repo_path else None
            ),
            "source_repo": self.source_repo,
            "worktree_path": str(self.worktree_path),
            "branch": self.branch,
            "registry_file": str(self.registry_file),
            "command": list(self.command),
            "document": self.document,
        }


@dataclass(frozen=True)
class WorktreeOwner:
    found: bool
    owner_ref: str
    owner_uid: str
    path: Path
    record_path: Path | None = None
    identity: str = ""
    name: str = ""
    lifecycle: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "owner_ref": self.owner_ref,
            "owner_uid": self.owner_uid,
            "path": str(self.path),
            "record_path": str(self.record_path) if self.record_path else None,
            "identity": self.identity,
            "name": self.name,
            "lifecycle": self.lifecycle,
        }


@dataclass(frozen=True)
class WorktreeOwnerMigration:
    name: str
    source_file: Path | None
    before_owner_ref: str
    after_owner_ref: str
    before_owner_uid: str
    after_owner_uid: str
    changed: bool
    applied: bool
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_file": str(self.source_file) if self.source_file else None,
            "before_owner_ref": self.before_owner_ref,
            "after_owner_ref": self.after_owner_ref,
            "before_owner_uid": self.before_owner_uid,
            "after_owner_uid": self.after_owner_uid,
            "changed": self.changed,
            "applied": self.applied,
            "status": self.status,
        }


def resolve_worktree_owner(
    path: Path,
    records: list[RegistryAsset],
) -> WorktreeOwner:
    resolved = path.expanduser().resolve()
    candidates = [
        record
        for record in records
        if record.kind == "Worktree"
        and record.path is not None
        and _is_under(resolved, record.path)
    ]
    if not candidates:
        return WorktreeOwner(found=False, owner_ref="", owner_uid="", path=resolved)

    record = max(candidates, key=lambda item: len(item.path.parts) if item.path else 0)
    return WorktreeOwner(
        found=True,
        owner_ref=record.spec.owner_ref,
        owner_uid=record.spec.owner_uid,
        path=resolved,
        record_path=record.path,
        identity=record.identity,
        name=record.name,
        lifecycle=record.spec.lifecycle,
    )


def migrate_worktree_owner_uids(
    records: list[RegistryAsset],
    *,
    dry_run: bool = False,
) -> list[WorktreeOwnerMigration]:
    results: list[WorktreeOwnerMigration] = []
    for record in records:
        if record.kind != "Worktree":
            continue
        result = _plan_owner_migration(record)
        if result.changed and not dry_run:
            _apply_owner_migration(record, result)
            result = WorktreeOwnerMigration(
                name=result.name,
                source_file=result.source_file,
                before_owner_ref=result.before_owner_ref,
                after_owner_ref=result.after_owner_ref,
                before_owner_uid=result.before_owner_uid,
                after_owner_uid=result.after_owner_uid,
                changed=True,
                applied=True,
                status="updated",
            )
        results.append(result)
    return results


def plan_create_worktree(  # noqa: PLR0913
    repo_path: Path,
    owner_ref: str,
    workspace_root: Path,
    registry_root: Path,
    records: list[RegistryAsset],
    *,
    slug: str = "",
    branch: str = "",
    path: Path | None = None,
    world: str = "",
    source_repo: str = "",
    allow_unowned: bool = False,
) -> WorktreePlan:
    source_path = repo_path.expanduser().resolve()
    if not source_path.exists():
        raise RegistrarError(f"source repo path does not exist: {source_path}")
    _ensure_git_repo(source_path, "source repo")

    owner = _normalize_owner_ref(owner_ref, allow_unowned=allow_unowned)
    name = _source_repo_name(source_path, source_repo)
    inferred_world = _infer_world(
        owner.ref, source_path, records, workspace_root, world
    )
    branch_name = _normalize_branch(branch or _default_branch(owner.ref, slug))
    worktree_path = (
        path.expanduser().resolve()
        if path is not None
        else _default_worktree_path(workspace_root, name, owner.ref, slug)
    )
    _ensure_worktree_target(worktree_path, workspace_root, must_exist=False)

    document = _worktree_document(
        worktree_path,
        owner.ref,
        owner.uid,
        inferred_world,
        source_repo=name,
    )
    registry_file = _registry_file(registry_root, worktree_path.name)
    _ensure_no_registry_conflict(records, registry_file, document, worktree_path)
    return WorktreePlan(
        action="create",
        owner_ref=owner.ref,
        owner_uid=owner.uid,
        world=inferred_world,
        source_repo_path=source_path,
        source_repo=name,
        worktree_path=worktree_path,
        branch=branch_name,
        registry_file=registry_file,
        document=document,
        command=(
            "git",
            "-C",
            str(source_path),
            "worktree",
            "add",
            "-b",
            branch_name,
            str(worktree_path),
            "HEAD",
        ),
    )


def plan_register_worktree(  # noqa: PLR0913
    worktree_path: Path,
    owner_ref: str,
    workspace_root: Path,
    registry_root: Path,
    records: list[RegistryAsset],
    *,
    world: str = "",
    source_repo: str = "",
    allow_unowned: bool = False,
) -> WorktreePlan:
    path = worktree_path.expanduser().resolve()
    if not path.exists():
        raise RegistrarError(f"worktree path does not exist: {path}")
    _ensure_git_repo(path, "worktree")
    _ensure_worktree_target(path, workspace_root, must_exist=True)

    owner = _normalize_owner_ref(owner_ref, allow_unowned=allow_unowned)
    name = source_repo.strip() or _infer_source_repo(path, owner.ref)
    inferred_world = _infer_world(
        owner.ref, _source_repo_path(path), records, workspace_root, world
    )
    branch_name = _git_stdout(path, "rev-parse", "--abbrev-ref", "HEAD")
    if branch_name == "HEAD":
        branch_name = ""

    document = _worktree_document(
        path,
        owner.ref,
        owner.uid,
        inferred_world,
        source_repo=name,
    )
    registry_file = _registry_file(registry_root, path.name)
    _ensure_no_registry_conflict(records, registry_file, document, path)
    return WorktreePlan(
        action="register",
        owner_ref=owner.ref,
        owner_uid=owner.uid,
        world=inferred_world,
        source_repo_path=_source_repo_path(path),
        source_repo=name,
        worktree_path=path,
        branch=branch_name,
        registry_file=registry_file,
        document=document,
    )


def apply_create_worktree(plan: WorktreePlan) -> WorktreePlan:
    if not plan.command:
        raise RegistrarError("create plan is missing git command")
    _run(plan.command)
    try:
        _write_registry_file(plan.registry_file, plan.document)
    except Exception:
        _run(
            (
                "git",
                "-C",
                str(plan.source_repo_path),
                "worktree",
                "remove",
                "--force",
                str(plan.worktree_path),
            )
        )
        raise
    commit_registry_change(plan.registry_file, _register_commit_message(plan))
    return _mark_applied(plan)


def apply_register_worktree(plan: WorktreePlan) -> WorktreePlan:
    _write_registry_file(plan.registry_file, plan.document)
    commit_registry_change(plan.registry_file, _register_commit_message(plan))
    return _mark_applied(plan)


def _register_commit_message(plan: WorktreePlan) -> str:
    return (
        f"chore(registrar): register worktree {plan.worktree_path.name} "
        f"({plan.owner_ref})"
    )


def render_document_yaml(plan: WorktreePlan) -> str:
    return yaml.safe_dump(plan.document, allow_unicode=True, sort_keys=False).strip()


def _mark_applied(plan: WorktreePlan) -> WorktreePlan:
    return WorktreePlan(
        action=plan.action,
        owner_ref=plan.owner_ref,
        owner_uid=plan.owner_uid,
        world=plan.world,
        source_repo_path=plan.source_repo_path,
        source_repo=plan.source_repo,
        worktree_path=plan.worktree_path,
        branch=plan.branch,
        registry_file=plan.registry_file,
        document=plan.document,
        command=plan.command,
        applied=True,
    )


def _plan_owner_migration(record: RegistryAsset) -> WorktreeOwnerMigration:
    current_ref = record.spec.owner_ref.strip()
    current_uid = record.spec.owner_uid.strip()
    if not current_ref:
        return _owner_migration_result(record, "", "", current_uid, "", "missing-owner")
    if current_ref.startswith("none:"):
        return _owner_migration_result(
            record, current_ref, current_ref, current_uid, current_uid, "unowned"
        )
    owner = _resolve_docket_owner(current_uid or current_ref)
    if not owner.uid:
        return _owner_migration_result(
            record, current_ref, current_ref, current_uid, current_uid, "unresolved"
        )
    next_ref = owner.ref or current_ref
    if _owner_record_current(record, next_ref, owner.uid):
        return _owner_migration_result(
            record, current_ref, next_ref, current_uid, owner.uid, "current"
        )
    return _owner_migration_result(
        record, current_ref, next_ref, current_uid, owner.uid, "planned"
    )


def _owner_migration_result(
    record: RegistryAsset,
    before_ref: str,
    after_ref: str,
    before_uid: str,
    after_uid: str,
    status: str,
) -> WorktreeOwnerMigration:
    return WorktreeOwnerMigration(
        name=record.name,
        source_file=record.source_file,
        before_owner_ref=before_ref,
        after_owner_ref=after_ref,
        before_owner_uid=before_uid,
        after_owner_uid=after_uid,
        changed=status == "planned",
        applied=False,
        status=status,
    )


def _owner_record_current(
    record: RegistryAsset, owner_ref: str, owner_uid: str
) -> bool:
    return (
        record.spec.owner_ref == owner_ref
        and record.spec.owner_uid == owner_uid
        and record.labels.get("issue") == owner_ref
        and record.labels.get("issue_uid") == owner_uid
    )


def _apply_owner_migration(
    record: RegistryAsset,
    migration: WorktreeOwnerMigration,
) -> None:
    if record.source_file is None:
        raise RegistrarError(f"{record.name}: registry source file is unknown")
    document = _read_registry_document(record.source_file)
    metadata = _ensure_mapping(document, "metadata", record.source_file)
    labels = metadata.get("labels")
    if labels is None:
        labels = {}
        metadata["labels"] = labels
    if not isinstance(labels, dict):
        raise RegistrarError(f"{record.source_file}: metadata.labels must be a mapping")
    spec = _ensure_mapping(document, "spec", record.source_file)
    spec["owner_ref"] = migration.after_owner_ref
    spec["owner_uid"] = migration.after_owner_uid
    labels["issue"] = migration.after_owner_ref
    labels["issue_uid"] = migration.after_owner_uid
    record.source_file.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    commit_registry_change(
        record.source_file,
        f"chore(registrar): migrate worktree owner uid {record.name}",
    )


def _read_registry_document(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RegistrarError(f"registry file must contain a mapping: {path}")
    return raw


def _ensure_mapping(
    document: dict[str, Any],
    key: str,
    source_file: Path,
) -> dict[str, Any]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise RegistrarError(f"{source_file}: {key} mapping is required")
    return value


def _worktree_document(
    path: Path,
    owner_ref: str,
    owner_uid: str,
    world: str,
    *,
    source_repo: str,
) -> dict[str, Any]:
    labels = {
        "world": world,
        "source_repo": source_repo,
        "role": "linked-worktree",
    }
    if not owner_ref.startswith("none:"):
        labels["issue"] = owner_ref
    if owner_uid:
        labels["issue_uid"] = owner_uid
    return {
        "apiVersion": API_VERSION,
        "kind": "Worktree",
        "metadata": {
            "identity": derive_identity(
                "Worktree", path.name, "workspace/worktrees", ""
            ),
            "name": path.name,
            "path": str(path),
            "labels": labels,
        },
        "spec": {
            "owner_ref": owner_ref,
            **({"owner_uid": owner_uid} if owner_uid else {}),
            "lifecycle": "active",
            "placement": "workspace/worktrees",
            "restore_policy": "linked-worktree",
            "allowed_actions": [
                "inspect",
                "relocate-dry-run",
                "closeout-dry-run",
            ],
            "closeout_policy": "require-finalizers",
        },
        "finalizers": [
            "pm-owner-required",
            "branch-preserved",
            "closeout-recorded",
            "principal-approval-required",
        ],
    }


def _normalize_owner_ref(
    owner_ref: str, *, allow_unowned: bool = False
) -> NormalizedOwner:
    value = owner_ref.strip()
    if not value:
        raise RegistrarError("--owner-ref is required")
    if value.startswith("none:") and len(value) > len("none:"):
        if not allow_unowned:
            raise RegistrarError(
                "--owner-ref none:<reason> is a break-glass exception; "
                "create or reuse a docket issue and pass --owner-ref <ISSUE-REF>. "
                "For an explicit temporary exception, rerun with --allow-unowned."
            )
        return NormalizedOwner(ref=value)
    if not re.fullmatch(r"[A-Z][A-Z0-9]+-\d+|dkt_[0-9a-f]{32}", value):
        raise RegistrarError(
            "--owner-ref must be a docket issue ref like WORK-12 or dkt_<uid>. "
            "If no issue exists, run `docket new --triage --actor codex <title>` "
            "and retry with that issue id."
        )
    return _resolve_docket_owner(value)


def _resolve_docket_owner(value: str) -> NormalizedOwner:
    try:
        result = subprocess.run(
            ["docket", "resolve", value, "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return NormalizedOwner(ref=value)
    if result.returncode != 0:
        return NormalizedOwner(ref=value)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return NormalizedOwner(ref=value)
    display = str(payload.get("display_ref") or "").strip()
    uid = str(payload.get("uid") or "").strip()
    return NormalizedOwner(ref=display or value, uid=uid)


def _default_branch(owner_ref: str, slug: str) -> str:
    branch = owner_ref.lower().replace("_", "-")
    suffix = _slugify(slug)
    return f"{branch}-{suffix}" if suffix else branch


def _source_repo_name(source_path: Path, override: str) -> str:
    return override.strip() or _remote_repo_name(source_path) or source_path.name


def _default_worktree_path(
    workspace_root: Path, source_repo: str, owner_ref: str, slug: str
) -> Path:
    repo_slug = _slugify(source_repo)
    owner_slug = _slugify(owner_ref)
    if not repo_slug:
        raise RegistrarError(
            f'cannot derive worktree name from source repo "{source_repo}"'
        )
    base_name = f"{repo_slug}-{owner_slug}"
    path = (workspace_root / "worktrees" / base_name).resolve()
    suffix = _slugify(slug)
    if path.exists() and suffix:
        return (workspace_root / "worktrees" / f"{base_name}-{suffix}").resolve()
    return path


def _normalize_branch(branch: str) -> str:
    value = _slugify(branch)
    if not value:
        raise RegistrarError("branch name must not be empty")
    return value


def _slugify(value: str) -> str:
    raw = value.strip().lower().replace("_", "-")
    raw = re.sub(r"[^a-z0-9.-]+", "-", raw)
    return re.sub(r"-+", "-", raw).strip("-")


def _infer_world(
    owner_ref: str,
    source_path: Path | None,
    records: list[RegistryAsset],
    workspace_root: Path,
    explicit: str,
) -> str:
    if explicit:
        if explicit not in {"personal", "work"}:
            raise RegistrarError("--world must be personal or work")
        return explicit
    if source_path is not None:
        for record in records:
            if record.path == source_path and record.labels.get("world"):
                return record.labels["world"]
        try:
            parts = source_path.resolve().relative_to(workspace_root.resolve()).parts
        except ValueError:
            parts = ()
        if (
            len(parts) >= 2  # noqa: PLR2004
            and parts[0] in {"sources", "knowledge", "data"}
            and parts[1] in {"personal", "work"}
        ):
            return parts[1]
    prefix = owner_ref.split("-", maxsplit=1)[0]
    if prefix in WORK_PREFIXES:
        return "work"
    if prefix in PERSONAL_PREFIXES:
        return "personal"
    raise RegistrarError("cannot infer world; pass --world personal or --world work")


def _source_repo_path(worktree_path: Path) -> Path | None:
    common_dir = _git_stdout(worktree_path, "rev-parse", "--git-common-dir")
    if not common_dir:
        return None
    path = Path(common_dir)
    if not path.is_absolute():
        path = (worktree_path / path).resolve()
    if path.name == ".git":
        return path.parent.resolve()
    return None


def _infer_source_repo(path: Path, owner_ref: str) -> str:
    remote_repo = _remote_repo_name(path)
    if remote_repo:
        return remote_repo
    source = _source_repo_path(path)
    if source is not None:
        return source.name
    branch_suffix = owner_ref.lower()
    name = path.name
    if name.endswith(f"-{branch_suffix}"):
        return name[: -(len(branch_suffix) + 1)]
    return name


def _remote_repo_name(path: Path) -> str:
    url = _git_stdout(path, "config", "--get", "remote.origin.url")
    if not url:
        return ""
    name = url.rstrip("/").rsplit("/", maxsplit=1)[-1]
    return name.removesuffix(".git")


def _registry_file(registry_root: Path, name: str) -> Path:
    return registry_root / "assets" / f"worktree-{name}.yaml"


def _ensure_worktree_target(
    path: Path, workspace_root: Path, *, must_exist: bool
) -> None:
    try:
        rel = path.resolve().relative_to(workspace_root.resolve())
    except ValueError as exc:
        raise RegistrarError(f"worktree path is outside workspace: {path}") from exc
    if len(rel.parts) < 2 or rel.parts[0] != "worktrees":  # noqa: PLR2004
        raise RegistrarError(
            f"worktree path must be under {workspace_root / 'worktrees'}"
        )
    if must_exist:
        if current_placement(path, workspace_root) != "workspace/worktrees":
            raise RegistrarError(
                f"path is not classified as workspace/worktrees: {path}"
            )
    elif path.exists():
        raise RegistrarError(f"worktree path already exists: {path}")


def _ensure_git_repo(path: Path, label: str) -> None:
    if _git_stdout(path, "rev-parse", "--show-toplevel") == "":
        raise RegistrarError(f"{label} is not a git repo: {path}")


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _ensure_no_registry_conflict(
    records: list[RegistryAsset],
    registry_file: Path,
    document: dict[str, Any],
    path: Path,
) -> None:
    identity = str(document["metadata"]["identity"])
    if registry_file.exists():
        raise RegistrarError(f"registry file already exists: {registry_file}")
    for record in records:
        if record.identity == identity:
            suffix = " tombstone" if record.kind == TOMBSTONE_KIND else ""
            raise RegistrarError(
                f"registry identity already exists{suffix}: {identity}"
            )
        if record.path == path:
            raise RegistrarError(f"registry path already exists: {path}")


def _write_registry_file(path: Path, document: dict[str, Any]) -> None:
    if path.exists():
        raise RegistrarError(f"registry file already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _git_stdout(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _run(command: tuple[str, ...]) -> None:
    result = subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RegistrarError(f"command failed: {' '.join(command)}\n{detail}")
