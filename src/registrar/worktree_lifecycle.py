"""Docket-driven worktree audit and closeout."""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from .errors import RegistrarError
from .git import commit_registry_change, git_status
from .model import API_VERSION, TOMBSTONE_KIND, RegistryAsset
from .registry import by_name_or_path
from .worktree import PERSONAL_PREFIXES, WORK_PREFIXES

WORKTREE_KIND = "Worktree"
OWNER_CLOSED_TYPES = {"completed", "canceled"}
OWNER_ACTIVE_TYPES = {"started", "unstarted"}
RecordMode = Literal["delete", "tombstone"]


@dataclass(frozen=True)
class OwnerInfo:
    ref: str
    state: str
    status: str = ""
    canonical_ref: str = ""
    uid: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "ref": self.ref,
            "state": self.state,
            "status": self.status,
            "canonical_ref": self.canonical_ref,
            "uid": self.uid,
        }


@dataclass(frozen=True)
class WorktreeAuditItem:
    name: str
    identity: str
    owner_ref: str
    owner_uid: str
    owner_canonical: str
    owner_state: str
    owner_status: str
    path: Path
    path_state: str
    branch: str
    default_branch: str
    branch_state: str
    dirty: bool
    untracked_count: int
    recommendation: str
    blocker: str
    close_gate_state: str
    close_gate_action: str
    source_file: Path | None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "identity": self.identity,
            "owner_ref": self.owner_ref,
            "owner_uid": self.owner_uid,
            "owner_canonical": self.owner_canonical,
            "owner_state": self.owner_state,
            "owner_status": self.owner_status,
            "path": str(self.path),
            "path_state": self.path_state,
            "branch": self.branch,
            "default_branch": self.default_branch,
            "branch_state": self.branch_state,
            "dirty": self.dirty,
            "untracked_count": self.untracked_count,
            "recommendation": self.recommendation,
            "blocker": self.blocker,
            "close_gate_state": self.close_gate_state,
            "close_gate_action": self.close_gate_action,
            "source_file": str(self.source_file) if self.source_file else "",
        }


@dataclass(frozen=True)
class WorktreeReconciliation:
    owner_refs: tuple[str, ...]
    blocked: bool
    total: int
    active_count: int
    ready_to_close_count: int
    blocked_count: int
    stale_count: int
    items: tuple[WorktreeAuditItem, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "owner_refs": list(self.owner_refs),
            "blocked": self.blocked,
            "total": self.total,
            "active_count": self.active_count,
            "ready_to_close_count": self.ready_to_close_count,
            "blocked_count": self.blocked_count,
            "stale_count": self.stale_count,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class WorktreeCloseoutResult:
    name: str
    path: Path
    applied: bool
    blocked: bool
    blockers: tuple[str, ...]
    actions: tuple[str, ...]
    record_mode: str
    source_file: Path | None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": str(self.path),
            "applied": self.applied,
            "blocked": self.blocked,
            "blockers": list(self.blockers),
            "actions": list(self.actions),
            "record_mode": self.record_mode,
            "source_file": str(self.source_file) if self.source_file else "",
        }


def audit_worktrees(
    records: list[RegistryAsset],
    workspace_root: Path,
    *,
    asset: str | None = None,
    owner_ref: str = "",
    include_retired: bool = False,
) -> list[WorktreeAuditItem]:
    selected = _select_worktree_records(
        records,
        workspace_root,
        asset=asset,
        owner_ref=owner_ref,
        include_retired=include_retired,
    )
    owner_cache: dict[tuple[str, str], OwnerInfo] = {}
    return [
        _audit_record(record, workspace_root, owner_cache)
        for record in selected
        if _is_auditable(record, include_retired)
    ]


def reconcile_owner_worktrees(
    records: list[RegistryAsset],
    workspace_root: Path,
    owner_refs: Sequence[str],
    *,
    include_retired: bool = False,
) -> WorktreeReconciliation:
    refs = tuple(dict.fromkeys(ref.strip() for ref in owner_refs if ref.strip()))
    if not refs:
        raise RegistrarError("worktree reconcile requires at least one owner ref")

    owner_cache: dict[tuple[str, str], OwnerInfo] = {}
    items = tuple(
        item
        for record in _reconciliation_candidates(
            records, workspace_root, refs, include_retired=include_retired
        )
        if _is_auditable(record, include_retired)
        for item in (_audit_record(record, workspace_root, owner_cache),)
        if _owner_ref_matches(item, refs)
    )
    active_items = tuple(item for item in items if item.close_gate_state != "retired")
    blocked_items = tuple(
        item for item in active_items if item.close_gate_state not in {"ready-to-close"}
    )
    return WorktreeReconciliation(
        owner_refs=refs,
        blocked=bool(active_items),
        total=len(items),
        active_count=len(active_items),
        ready_to_close_count=sum(
            1 for item in active_items if item.close_gate_state == "ready-to-close"
        ),
        blocked_count=len(blocked_items),
        stale_count=sum(1 for item in active_items if item.close_gate_state == "stale"),
        items=active_items,
    )


def _reconciliation_candidates(
    records: list[RegistryAsset],
    workspace_root: Path,
    refs: Sequence[str],
    *,
    include_retired: bool,
) -> list[RegistryAsset]:
    """Return the narrow safe set for an owner reconciliation.

    New worktree records carry the immutable docket owner_uid, so matching them
    needs no subprocess-backed owner resolution. Pre-identity records have no
    owner_uid; retain the exhaustive compatibility path for those records so a
    legacy alias can never evade the close gate.
    """
    selected = _select_worktree_records(
        records,
        workspace_root,
        asset=None,
        owner_ref="",
        include_retired=include_retired,
    )
    ref_set = set(refs)
    return [
        record
        for record in selected
        if not record.spec.owner_uid
        or record.spec.owner_ref in ref_set
        or record.spec.owner_uid in ref_set
    ]


def closeout_worktree(  # noqa: PLR0913
    records: list[RegistryAsset],
    asset: str,
    workspace_root: Path,
    *,
    apply: bool,
    owner_active_ok: bool = False,
    allow_unmerged: bool = False,
    stale_record: bool = False,
    force: bool = False,
    delete_branch: bool = False,
    record_mode: RecordMode = "delete",
) -> WorktreeCloseoutResult:
    if record_mode not in {"delete", "tombstone"}:
        raise RegistrarError("--record must be delete or tombstone")
    record = by_name_or_path(records, asset)
    if record is None:
        raise RegistrarError("worktree closeout requires a registered worktree record")
    if record.kind == TOMBSTONE_KIND:
        path = Path(record.labels.get("old_path", str(workspace_root / record.name)))
        return WorktreeCloseoutResult(
            name=record.name,
            path=path,
            applied=False,
            blocked=False,
            blockers=(),
            actions=("already closed out; tombstone recorded",),
            record_mode="tombstone",
            source_file=record.source_file,
        )
    if record.kind != WORKTREE_KIND:
        raise RegistrarError(
            f"{record.name}: expected Worktree record, got {record.kind}"
        )
    item = _audit_record(record, workspace_root, {})
    blockers = _closeout_blockers(
        item,
        owner_active_ok=owner_active_ok,
        allow_unmerged=allow_unmerged,
        stale_record=stale_record,
        force=force,
    )
    if blockers:
        return WorktreeCloseoutResult(
            name=record.name,
            path=item.path,
            applied=False,
            blocked=True,
            blockers=tuple(blockers),
            actions=("resolve blockers", "rerun worktree closeout --dry-run"),
            record_mode=record_mode,
            source_file=record.source_file,
        )

    actions = _planned_actions(
        item, record_mode, force=force, delete_branch=delete_branch
    )
    if not apply:
        return WorktreeCloseoutResult(
            name=record.name,
            path=item.path,
            applied=False,
            blocked=False,
            blockers=(),
            actions=tuple(actions),
            record_mode=record_mode,
            source_file=record.source_file,
        )

    if item.path_state == "exists":
        main_repo = _main_worktree_path(item.path) if delete_branch else None
        _remove_git_worktree(item.path, force=force)
        if delete_branch and main_repo is not None:
            _delete_local_branch(main_repo, item.branch, item.default_branch)
    _closeout_record(record, record_mode)
    if record.source_file is not None:
        commit_registry_change(
            record.source_file,
            f"chore(registrar): closeout worktree {record.name} ({record_mode})",
        )
    return WorktreeCloseoutResult(
        name=record.name,
        path=item.path,
        applied=True,
        blocked=False,
        blockers=(),
        actions=tuple(actions),
        record_mode=record_mode,
        source_file=record.source_file,
    )


def _select_worktree_records(
    records: list[RegistryAsset],
    workspace_root: Path,
    *,
    asset: str | None,
    owner_ref: str,
    include_retired: bool,
) -> list[RegistryAsset]:
    if asset:
        record = by_name_or_path(records, asset)
        if record is None:
            path = Path(asset).expanduser()
            if not path.is_absolute():
                path = workspace_root / asset
            raise RegistrarError(f"no registered worktree record for {path}")
        return [record]
    selected = [
        record
        for record in records
        if _is_auditable(record, include_retired)
        and _record_matches_owner(record, owner_ref)
    ]
    return sorted(selected, key=lambda item: item.name)


def _record_matches_owner(record: RegistryAsset, owner_ref: str) -> bool:
    if not owner_ref:
        return True
    return owner_ref in {record.spec.owner_ref, record.spec.owner_uid}


def _is_auditable(record: RegistryAsset, include_retired: bool) -> bool:
    if record.kind == WORKTREE_KIND:
        return True
    return include_retired and record.kind == TOMBSTONE_KIND


def _audit_record(
    record: RegistryAsset,
    workspace_root: Path,
    owner_cache: dict[tuple[str, str], OwnerInfo],
) -> WorktreeAuditItem:
    path = _record_path(record, workspace_root)
    path_state = "exists" if path.exists() else "missing"
    git = git_status(path)
    default_branch = _default_branch(path) if git.is_repo else ""
    branch_state = (
        _branch_state(path, git.branch, default_branch) if git.is_repo else "not-repo"
    )
    world = record.labels.get("world", "")
    owner = _owner_info(
        record.spec.owner_ref,
        owner_cache,
        world=world,
        owner_uid=record.spec.owner_uid,
    )
    recommendation, blocker = _recommend(
        record, path_state, owner, git.dirty, git.untracked_count, branch_state
    )
    close_gate_state, close_gate_action = _close_gate(
        record,
        path_state,
        git.dirty,
        git.untracked_count,
        branch_state,
        git.branch,
        default_branch,
    )
    return WorktreeAuditItem(
        name=record.name,
        identity=record.identity,
        owner_ref=record.spec.owner_ref,
        owner_uid=record.spec.owner_uid,
        owner_canonical=owner.canonical_ref,
        owner_state=owner.state,
        owner_status=owner.status,
        path=path,
        path_state=path_state,
        branch=git.branch,
        default_branch=default_branch,
        branch_state=branch_state,
        dirty=git.dirty,
        untracked_count=git.untracked_count,
        recommendation=recommendation,
        blocker=blocker,
        close_gate_state=close_gate_state,
        close_gate_action=close_gate_action,
        source_file=record.source_file,
    )


def _record_path(record: RegistryAsset, workspace_root: Path) -> Path:
    if record.path is not None:
        return record.path
    return Path(
        record.labels.get("old_path", str(workspace_root / record.name))
    ).expanduser()


def _owner_info(
    owner_ref: str,
    cache: dict[tuple[str, str], OwnerInfo],
    world: str = "",
    owner_uid: str = "",
) -> OwnerInfo:
    if not owner_ref:
        return OwnerInfo(ref="", state="missing")
    if owner_ref.startswith("none:"):
        return OwnerInfo(ref=owner_ref, state="none", status=owner_ref)
    lookup = owner_uid or owner_ref
    docket_tier = _docket_tier_for(owner_ref, world)
    cache_key = (docket_tier, lookup)
    if cache_key not in cache:
        cache[cache_key] = _read_docket_owner(
            lookup, docket_tier=docket_tier, fallback_ref=owner_ref
        )
    return cache[cache_key]


def _docket_tier_for(owner_ref: str, world: str) -> str:
    if world in {"personal", "work"}:
        return world
    prefix = owner_ref.split("-", maxsplit=1)[0]
    if prefix in WORK_PREFIXES:
        return "work"
    if prefix in PERSONAL_PREFIXES:
        return "personal"
    return ""


def _read_docket_owner(
    owner_ref: str, *, docket_tier: str = "", fallback_ref: str = ""
) -> OwnerInfo:
    result = _run_docket(_docket_cmd(docket_tier, "resolve", owner_ref, "--json"))
    if result is None:
        return OwnerInfo(ref=owner_ref, state="unknown")
    if result.returncode == 0:
        owner = _owner_from_resolve_json(result.stdout, owner_ref, fallback_ref)
        if owner is not None:
            return owner

    result = _run_docket(_docket_cmd(docket_tier, "show", fallback_ref or owner_ref))
    if result is None or result.returncode != 0:
        return OwnerInfo(ref=owner_ref, state="unknown")
    return _owner_from_show_output(result.stdout, owner_ref, fallback_ref)


def _docket_cmd(docket_tier: str, *args: str) -> list[str]:
    cmd = ["docket"]
    if docket_tier:
        cmd.extend(["--tier", docket_tier])
    cmd.extend(args)
    return cmd


def _run_docket(cmd: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _owner_from_resolve_json(
    stdout: str, owner_ref: str, fallback_ref: str
) -> OwnerInfo | None:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not payload:
        return None
    return OwnerInfo(
        ref=str(payload.get("display_ref") or fallback_ref or owner_ref),
        state=str(payload.get("state_type") or "unknown"),
        status=str(payload.get("status") or ""),
        canonical_ref=str(payload.get("id") or ""),
        uid=str(payload.get("uid") or ""),
    )


def _owner_from_show_output(
    stdout: str, owner_ref: str, fallback_ref: str
) -> OwnerInfo:
    status = ""
    state_type = ""
    canonical = ""
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("id:"):
            canonical = line.split(":", maxsplit=1)[1].strip()
        elif line.startswith("status:"):
            status = line.split(":", maxsplit=1)[1].strip()
        elif line.startswith("state_type:"):
            state_type = line.split(":", maxsplit=1)[1].strip()
    return OwnerInfo(
        ref=fallback_ref or owner_ref,
        state=state_type or "unknown",
        status=status,
        canonical_ref=canonical,
    )


def _recommend(  # noqa: PLR0913, PLR0911
    record: RegistryAsset,
    path_state: str,
    owner: OwnerInfo,
    dirty: bool,
    untracked_count: int,
    branch_state: str,
) -> tuple[str, str]:
    if record.kind == TOMBSTONE_KIND:
        return ("retired", "")
    if path_state == "missing":
        return ("stale: remove active record", "")
    if owner.state in OWNER_ACTIVE_TYPES:
        return ("keep: owner active", "")
    if dirty:
        return ("blocked: dirty", "dirty")
    if untracked_count:
        return ("blocked: untracked", "untracked")
    if owner.state in {"missing", "none"}:
        return ("blocked: owner gap", "owner")
    if owner.state == "unknown":
        return ("review: owner state unknown", "owner")
    if owner.state not in OWNER_CLOSED_TYPES:
        return ("keep: owner active", "")
    if branch_state in {"unmerged", "unknown", "detached"}:
        return (f"blocked: branch {branch_state}", "branch")
    return ("closeable", "")


def _close_gate(  # noqa: PLR0913
    record: RegistryAsset,
    path_state: str,
    dirty: bool,
    untracked_count: int,
    branch_state: str,
    branch: str,
    default_branch: str,
) -> tuple[str, str]:
    if record.kind == TOMBSTONE_KIND:
        return ("retired", "already retired")
    if path_state == "missing":
        return (
            "stale",
            f"registrar worktree closeout {record.name} --apply "
            "--stale-record --owner-active-ok",
        )
    if dirty:
        return ("dirty", "commit, stash, or discard tracked changes before closeout")
    if untracked_count:
        return ("untracked", "remove, commit, or explicitly discard untracked files")
    if branch_state in {"unmerged", "unknown", "detached", "not-repo"}:
        return (
            branch_state,
            f"merge or discard, then: registrar worktree closeout {record.name} "
            "--apply --owner-active-ok --allow-unmerged --delete-branch",
        )
    command = f"registrar worktree closeout {record.name} --apply --owner-active-ok"
    if branch and branch != default_branch:
        command += " --delete-branch"
    return ("ready-to-close", command)


def _owner_ref_matches(item: WorktreeAuditItem, refs: Sequence[str]) -> bool:
    ref_set = set(refs)
    if (
        item.owner_ref in ref_set
        or item.owner_uid in ref_set
        or item.owner_canonical in ref_set
    ):
        return True
    return bool(_owner_tokens(item.owner_canonical) & ref_set)


def _owner_tokens(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z]+-\d+", text))


def _closeout_blockers(
    item: WorktreeAuditItem,
    *,
    owner_active_ok: bool,
    allow_unmerged: bool,
    stale_record: bool,
    force: bool = False,
) -> list[str]:
    blockers: list[str] = []
    if item.path_state == "missing":
        if not stale_record:
            blockers.append("stale-record-required")
        return blockers
    if item.dirty and not force:
        blockers.append("dirty")
    if item.untracked_count and not force:
        blockers.append("untracked")
    if item.owner_state not in OWNER_CLOSED_TYPES and not owner_active_ok:
        blockers.append("owner-not-closed")
    if item.branch_state in {"unmerged", "unknown", "detached"} and not allow_unmerged:
        blockers.append(f"branch-{item.branch_state}")
    if item.branch_state == "not-repo":
        blockers.append("not-git-repo")
    return blockers


def _planned_actions(
    item: WorktreeAuditItem,
    record_mode: str,
    *,
    force: bool = False,
    delete_branch: bool = False,
) -> list[str]:
    actions = []
    if item.path_state == "exists":
        flag = " --force" if force else ""
        actions.append(f"git worktree remove{flag} {item.path}")
    else:
        actions.append("remove stale active record")
    if record_mode == "delete":
        actions.append("delete registrar active record")
    else:
        actions.append("convert registrar active record to Tombstone")
    if delete_branch and item.branch and item.branch != item.default_branch:
        actions.append(f"delete git branch {item.branch}")
    else:
        actions.append("preserve git branch")
    return actions


def _default_branch(path: Path) -> str:
    origin_head = _git_stdout(
        path, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"
    )
    if origin_head.startswith("origin/"):
        branch = origin_head.split("/", maxsplit=1)[1]
        if _local_branch_exists(path, branch):
            return branch
    for branch in ("main", "master"):
        if _local_branch_exists(path, branch):
            return branch
    return ""


def _branch_state(path: Path, branch: str, default_branch: str) -> str:
    if not branch:
        return "detached"
    if not default_branch:
        return "unknown"
    if branch == default_branch:
        return "default"
    result = subprocess.run(
        ["git", "-C", str(path), "merge-base", "--is-ancestor", "HEAD", default_branch],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return "merged" if result.returncode == 0 else "unmerged"


def _local_branch_exists(path: Path, branch: str) -> bool:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.returncode == 0


def _remove_git_worktree(path: Path, *, force: bool = False) -> None:
    cmd = ["git", "-C", str(path), "worktree", "remove"]
    if force:
        cmd.append("--force")
    cmd.append(str(path))
    result = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise RegistrarError(result.stderr.strip() or "git worktree remove failed")


def _main_worktree_path(path: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(path), "worktree", "list", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            return Path(line.split(" ", maxsplit=1)[1])
    return None


def _delete_local_branch(main_repo: Path, branch: str, default_branch: str) -> None:
    # best-effort: the worktree is already gone; never delete the default branch.
    if not branch or branch == default_branch:
        return
    subprocess.run(
        ["git", "-C", str(main_repo), "branch", "-D", branch],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _closeout_record(record: RegistryAsset, record_mode: str) -> None:
    if record.source_file is None:
        raise RegistrarError(f"{record.name}: registry source_file is unavailable")
    if record_mode == "delete":
        record.source_file.unlink()
        return
    record.source_file.write_text(
        yaml.safe_dump(
            _tombstone_document(record), allow_unicode=True, sort_keys=False
        ),
        encoding="utf-8",
    )


def _tombstone_document(record: RegistryAsset) -> dict[str, object]:
    labels = dict(record.labels)
    old_path = (
        str(record.path) if record.path is not None else labels.get("old_path", "")
    )
    labels.update(
        {
            "old_identity": record.identity,
            "old_path": old_path,
            "retired_reason": "worktree-closeout",
            "closeout_date": dt.date.today().isoformat(),
        }
    )
    return {
        "apiVersion": API_VERSION,
        "kind": TOMBSTONE_KIND,
        "metadata": {
            "identity": record.identity,
            "name": record.name,
            "labels": labels,
        },
        "spec": {
            "owner_ref": record.spec.owner_ref,
            **({"owner_uid": record.spec.owner_uid} if record.spec.owner_uid else {}),
            "lifecycle": "retired",
            "placement": "removed",
            "restore_policy": "remote-branch",
            "allowed_actions": ["inspect", "closeout-dry-run"],
        },
        "finalizers": list(record.finalizers),
    }


def _git_stdout(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip() if result.returncode == 0 else ""
