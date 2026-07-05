"""Shared data model."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

API_VERSION = "registrar.local/v1alpha1"
CAPABILITY_KIND = "Capability"
TOMBSTONE_KIND = "Tombstone"


@dataclass(frozen=True)
class CapabilityExposure:
    type: str
    name: str
    target: str = ""
    state: str = "active"
    policy: str = ""

    def to_dict(self) -> dict[str, str]:
        data = {
            "type": self.type,
            "name": self.name,
            "state": self.state,
        }
        if self.target:
            data["target"] = self.target
        if self.policy:
            data["policy"] = self.policy
        return data


@dataclass(frozen=True)
class GitStatus:
    is_repo: bool
    branch: str
    dirty: bool
    untracked_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_repo": self.is_repo,
            "branch": self.branch,
            "dirty": self.dirty,
            "untracked_count": self.untracked_count,
        }


@dataclass(frozen=True)
class InventoryAsset:
    kind: str
    name: str
    path: Path
    current_placement: str
    git: GitStatus
    labels: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "apiVersion": API_VERSION,
            "kind": self.kind,
            "metadata": {
                "name": self.name,
                "path": str(self.path),
                "labels": self.labels,
            },
            "status": {
                "exists": self.path.exists(),
                "current_placement": self.current_placement,
                "git_branch": self.git.branch,
                "git_dirty": self.git.dirty,
                "untracked_count": self.git.untracked_count,
            },
        }


@dataclass(frozen=True)
class AssetSpec:
    owner_ref: str
    lifecycle: str
    placement: str
    restore_policy: str
    allowed_actions: tuple[str, ...] = ()
    closeout_policy: str = ""
    capability_type: str = ""
    exposures: tuple[CapabilityExposure, ...] = ()
    implementation_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "owner_ref": self.owner_ref,
            "lifecycle": self.lifecycle,
            "placement": self.placement,
            "restore_policy": self.restore_policy,
        }
        if self.allowed_actions:
            data["allowed_actions"] = list(self.allowed_actions)
        if self.closeout_policy:
            data["closeout_policy"] = self.closeout_policy
        if self.capability_type:
            data["capability_type"] = self.capability_type
        if self.exposures:
            data["exposures"] = [exposure.to_dict() for exposure in self.exposures]
        if self.implementation_refs:
            data["implementation_refs"] = list(self.implementation_refs)
        return data


@dataclass(frozen=True)
class RegistryAsset:
    kind: str
    identity: str
    name: str
    path: Path | None
    spec: AssetSpec
    aliases: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)
    finalizers: tuple[str, ...] = ()
    source_file: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        metadata: dict[str, object] = {
            "identity": self.identity,
            "name": self.name,
            "labels": self.labels,
        }
        if self.aliases:
            metadata["aliases"] = list(self.aliases)
        if self.path is not None:
            metadata["path"] = str(self.path)
        return {
            "apiVersion": API_VERSION,
            "kind": self.kind,
            "metadata": metadata,
            "spec": self.spec.to_dict(),
            "finalizers": list(self.finalizers),
        }


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    name: str
    path: Path
    reason: str
    next_action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "name": self.name,
            "path": str(self.path),
            "reason": self.reason,
            "next_action": self.next_action,
        }


@dataclass(frozen=True)
class Ref:
    source: Path
    kind: str
    detail: str
    category: str = "rewrite"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "kind": self.kind,
            "detail": self.detail,
            "category": self.category,
        }


@dataclass(frozen=True)
class RelocatePlan:
    identity: str
    name: str
    source_path: Path
    target_path: Path
    reason: str
    affected_refs: tuple[Ref, ...]
    registry_diff: dict[str, Any]
    rollback_path: Path
    finalizers: tuple[str, ...]
    workspace_sweep_refs: tuple[Ref, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "name": self.name,
            "source_path": str(self.source_path),
            "target_path": str(self.target_path),
            "reason": self.reason,
            "affected_refs": [ref.to_dict() for ref in self.affected_refs],
            "workspace_sweep_refs": [
                ref.to_dict() for ref in self.workspace_sweep_refs
            ],
            "registry_diff": self.registry_diff,
            "rollback_path": str(self.rollback_path),
            "finalizers": list(self.finalizers),
        }


@dataclass(frozen=True)
class RelocateResult:
    identity: str
    name: str
    source_path: Path
    target_path: Path
    moved: bool
    rewritten_refs: tuple[Ref, ...]
    preserved_refs: tuple[Ref, ...]
    registry_file: Path | None
    repos_to_commit: tuple[str, ...]
    launchd_reload: tuple[str, ...]
    verified: bool
    remaining_refs: tuple[Ref, ...]
    rollback: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "name": self.name,
            "source_path": str(self.source_path),
            "target_path": str(self.target_path),
            "moved": self.moved,
            "rewritten_refs": [ref.to_dict() for ref in self.rewritten_refs],
            "preserved_refs": [ref.to_dict() for ref in self.preserved_refs],
            "registry_file": str(self.registry_file) if self.registry_file else None,
            "repos_to_commit": list(self.repos_to_commit),
            "launchd_reload": list(self.launchd_reload),
            "verified": self.verified,
            "remaining_refs": [ref.to_dict() for ref in self.remaining_refs],
            "rollback": list(self.rollback),
        }


@dataclass(frozen=True)
class CloseoutPlan:
    name: str
    path: Path
    blocked: bool
    finalizers: tuple[str, ...]
    actions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "blocked": self.blocked,
            "finalizers": list(self.finalizers),
            "actions": list(self.actions),
        }
