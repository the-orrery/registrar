"""Doctor checks over inventory plus desired registry state."""

from __future__ import annotations

from pathlib import Path

from .inventory import scan_inventory
from .model import Finding, RegistryAsset
from .paths import current_placement
from .registry import index_records


def run_doctor(  # noqa: C901
    workspace_root: Path,
    records: list[RegistryAsset],
    external_root: Path | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    record_index = index_records(records)
    observations = scan_inventory(workspace_root, external_root)

    for observation in observations:
        record = record_index.get(str(observation.path))
        if record is None:
            candidate = record_index.get(observation.name)
            if (
                candidate is not None
                and candidate.path is not None
                and observation.current_placement != "external-readonly"
                and not candidate.path.exists()
            ):
                record = candidate
        if record is None:
            next_action = "add a registry record or mark owner_ref as none:<reason>"
            if observation.current_placement == "workspace/worktrees":
                next_action = (
                    "run registrar worktree register "
                    f"{observation.path} --owner-ref <ISSUE|none:reason>"
                )
            findings.append(
                Finding(
                    code="no-owner",
                    severity="warning",
                    name=observation.name,
                    path=observation.path,
                    reason="no registry record with spec.owner_ref",
                    next_action=next_action,
                )
            )
            if observation.git.dirty:
                dirty_next_action = "assign owner_ref before closeout or relocation"
                if observation.current_placement == "workspace/worktrees":
                    dirty_next_action = (
                        "run registrar worktree register "
                        f"{observation.path} --owner-ref <ISSUE|none:reason>"
                    )
                findings.append(
                    Finding(
                        code="dirty-no-owner",
                        severity="error",
                        name=observation.name,
                        path=observation.path,
                        reason="git tree has local changes but no lifecycle owner",
                        next_action=dirty_next_action,
                    )
                )
            continue

        _check_record_vs_observation(findings, record, observation.current_placement)

    observed_paths = {str(item.path) for item in observations}
    for record in records:
        if record.path is None:
            continue
        if str(record.path) not in observed_paths and not record.path.exists():
            findings.append(
                Finding(
                    code="missing-path",
                    severity="error",
                    name=record.name,
                    path=record.path,
                    reason="registry metadata.path does not exist",
                    next_action="update metadata.path, add tombstone, or restore the asset",
                )
            )
        else:
            placement = current_placement(record.path, workspace_root)
            _check_record_vs_observation(findings, record, placement)

    return _dedupe(findings)


def _check_record_vs_observation(
    findings: list[Finding],
    record: RegistryAsset,
    observed_placement: str,
) -> None:
    if record.path is None:
        return
    if not record.spec.owner_ref:
        findings.append(
            Finding(
                code="no-owner",
                severity="warning",
                name=record.name,
                path=record.path,
                reason="registry record has empty spec.owner_ref",
                next_action="set owner_ref to a PM issue or none:<reason>",
            )
        )
    if record.spec.placement and record.spec.placement != observed_placement:
        findings.append(
            Finding(
                code="placement-drift",
                severity="warning",
                name=record.name,
                path=record.path,
                reason=(
                    f"spec.placement={record.spec.placement} but "
                    f"current_placement={observed_placement}"
                ),
                next_action="run registrar relocate --dry-run for this asset",
            )
        )


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, str, str]] = set()
    output: list[Finding] = []
    for finding in findings:
        key = (finding.code, finding.name, str(finding.path))
        if key not in seen:
            seen.add(key)
            output.append(finding)
    return output
