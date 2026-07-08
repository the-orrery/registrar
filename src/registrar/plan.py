"""Dry-run planners."""

from __future__ import annotations

from pathlib import Path

from .errors import RegistrarError
from .git import git_status
from .model import (
    CAPABILITY_KIND,
    TOMBSTONE_KIND,
    CloseoutPlan,
    RegistryAsset,
    RelocatePlan,
)
from .paths import current_placement, target_for_placement
from .refs import scan_affected_refs
from .registry import by_name_or_path


def relocate_plan(
    records: list[RegistryAsset],
    asset: str,
    workspace_root: Path,
) -> RelocatePlan:
    record = by_name_or_path(records, asset)
    if record is None:
        raise RegistrarError(
            "relocate requires a registry record; run doctor/seed before planning"
        )
    if record.kind == CAPABILITY_KIND:
        raise RegistrarError(
            f"{record.name}: Capability is not a filesystem relocation target; "
            "relocate the implementation repo/service instead"
        )
    if record.path is None:
        raise RegistrarError(f"{record.name}: metadata.path is required for relocate")
    if not record.spec.placement:
        raise RegistrarError(f"{record.name}: spec.placement is required")

    source = record.path
    observed = current_placement(source, workspace_root)
    target = target_for_placement(record.name, record.spec.placement, workspace_root)
    reason = (
        "placement already matches"
        if observed == record.spec.placement
        else f"spec.placement={record.spec.placement}, current_placement={observed}"
    )
    affected_refs = tuple(scan_affected_refs(source, workspace_root))
    finalizers = (
        "target-placement-approved",
        "relocate-plan-reviewed",
        "live-ref-cleared" if affected_refs else "live-ref-scan-clean",
        "closeout-recorded",
    )
    return RelocatePlan(
        identity=record.identity,
        name=record.name,
        source_path=source,
        target_path=target,
        reason=reason,
        affected_refs=affected_refs,
        registry_diff={
            "metadata.path": {"from": str(source), "to": str(target)},
            "status.current_placement": {
                "from": observed,
                "to": record.spec.placement,
            },
        },
        rollback_path=source,
        finalizers=finalizers,
    )


def closeout_plan(
    records: list[RegistryAsset],
    asset: str,
    workspace_root: Path,
) -> CloseoutPlan:
    record = by_name_or_path(records, asset)
    if record is None:
        path = Path(asset).expanduser()
        if not path.is_absolute():
            path = workspace_root / asset
        name = path.name
        git = git_status(path)
        finalizers = ["pm-owner-required"]
    else:
        if record.kind == TOMBSTONE_KIND:
            path = Path(
                record.labels.get("old_path", str(workspace_root / record.name))
            ).expanduser()
            return CloseoutPlan(
                name=record.name,
                path=path,
                blocked=False,
                finalizers=(),
                actions=("already closed out; tombstone recorded",),
            )
        if record.kind == CAPABILITY_KIND:
            raise RegistrarError(
                f"{record.name}: Capability is not a filesystem closeout target; "
                "close out the implementation repo/service instead"
            )
        if record.path is None:
            raise RegistrarError(
                f"{record.name}: metadata.path is required for closeout"
            )
        path = record.path
        name = record.name
        git = git_status(path)
        finalizers = list(record.finalizers)
        if not record.spec.owner_ref:
            finalizers.append("pm-owner-required")

    if git.dirty:
        finalizers.append("clean-or-explicit-dirty-owner")
    if git.is_repo and git.branch and git.branch not in {"main", "master"}:
        finalizers.append("branch-preserved")
    if scan_affected_refs(path, workspace_root):
        finalizers.append("live-ref-cleared")
    finalizers.extend(["closeout-recorded", "human-approved-destructive"])

    deduped = tuple(dict.fromkeys(finalizers))
    return CloseoutPlan(
        name=name,
        path=path,
        blocked=bool(deduped),
        finalizers=deduped,
        actions=(
            "review finalizers",
            "record owner decision",
            "rerun closeout --dry-run after blockers clear",
        ),
    )
