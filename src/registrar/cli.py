"""registrar CLI."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer
from orrery_heartbeat import check_update

from . import __version__
from .apply import apply_relocate
from .doctor import run_doctor
from .errors import RegistrarError
from .inventory import scan_inventory
from .model import CAPABILITY_KIND, Ref, RegistryAsset, RelocateResult
from .paths import (
    default_external_root,
    default_registry_root,
    default_workspace_root,
    expand,
)
from .plan import closeout_plan, relocate_plan
from .refs import scan_workspace_sweep_refs
from .registry import load_registry
from .render import render_json, table
from .seed import render_seed_yaml, seed_documents
from .worktree import (
    WorktreeOwner,
    WorktreePlan,
    apply_create_worktree,
    apply_register_worktree,
    plan_create_worktree,
    plan_register_worktree,
    render_document_yaml,
    resolve_worktree_owner,
)
from .worktree_lifecycle import (
    WorktreeAuditItem,
    WorktreeCloseoutResult,
    audit_worktrees,
    closeout_worktree,
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode=None,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="registrar — local workspace registry and lifecycle dry-run control plane.",
)
worktree_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode=None,
    help="create or register owned workspace worktrees.",
)
app.add_typer(worktree_app, name="worktree")

_GLOBAL_REGISTRY_ROOT: Path | None = None


WorkspaceOpt = Annotated[
    Path | None,
    typer.Option("--workspace-root", help="workspace root; defaults to ~/workspace"),
]
RegistryOpt = Annotated[
    Path | None,
    typer.Option(
        "--registry-root",
        help=(
            "registry data root; defaults to REGISTRAR_REGISTRY_ROOT or "
            "~/workspace/data/personal/registrar"
        ),
    ),
]
ExternalOpt = Annotated[
    Path | None,
    typer.Option(
        "--external-root",
        help="external readonly root; defaults to ~/external-readonly",
    ),
]
FormatOpt = Annotated[
    str,
    typer.Option("--format", "-f", help="table or json"),
]
WorktreeFormatOpt = Annotated[
    str,
    typer.Option("--format", "-f", help="table, json, or yaml"),
]


@app.callback()
def _callback(
    registry_root: RegistryOpt = None,
    version: bool = typer.Option(False, "--version", help="print version and exit"),
) -> None:
    global _GLOBAL_REGISTRY_ROOT  # noqa: PLW0603
    _GLOBAL_REGISTRY_ROOT = registry_root
    if version:
        print(__version__)
        raise typer.Exit


@app.command("inventory")
def _inventory(
    workspace_root: WorkspaceOpt = None,
    registry_root: RegistryOpt = None,
    external_root: ExternalOpt = None,
    output_format: FormatOpt = "table",
) -> None:
    """scan workspace/external refs and print generated status."""
    workspace = expand(workspace_root or default_workspace_root())
    external = expand(external_root or default_external_root())
    records = load_registry(_registry_root(registry_root))
    assets = scan_inventory(workspace, external)
    if output_format == "json":
        print(render_json(assets))
        return
    if output_format != "table":
        raise RegistrarError("--format must be table or json")

    record_paths = {str(record.path) for record in records if record.path is not None}
    rows = [
        {
            "kind": asset.kind,
            "name": asset.name,
            "placement": asset.current_placement,
            "git": _git_cell(asset.git.dirty, asset.git.branch),
            "registry": "yes" if str(asset.path) in record_paths else "no",
            "path": str(asset.path),
        }
        for asset in assets
    ]
    print(table(rows, ["kind", "name", "placement", "git", "registry", "path"]))


@app.command("doctor")
def _doctor(
    workspace_root: WorkspaceOpt = None,
    registry_root: RegistryOpt = None,
    external_root: ExternalOpt = None,
    output_format: FormatOpt = "table",
) -> None:
    """report owner, dirty-state, missing-path, and placement drift findings."""
    workspace = expand(workspace_root or default_workspace_root())
    external = expand(external_root or default_external_root())
    records = load_registry(_registry_root(registry_root))
    findings = run_doctor(workspace, records, external)
    if output_format == "json":
        print(render_json(findings))
        return
    if output_format != "table":
        raise RegistrarError("--format must be table or json")

    rows = [
        {
            "severity": finding.severity,
            "code": finding.code,
            "name": finding.name,
            "reason": finding.reason,
            "next": finding.next_action,
        }
        for finding in findings
    ]
    print(table(rows, ["severity", "code", "name", "reason", "next"]))


@app.command("capabilities")
def _capabilities(
    registry_root: RegistryOpt = None,
    output_format: FormatOpt = "table",
) -> None:
    """print capability records; does not scan or relocate filesystem paths."""
    records = load_registry(_registry_root(registry_root))
    capabilities = [record for record in records if record.kind == CAPABILITY_KIND]
    if output_format == "json":
        print(render_json(_CapabilityJson(capabilities)))
        return
    if output_format != "table":
        raise RegistrarError("--format must be table or json")

    rows = [
        {
            "name": record.name,
            "identity": record.identity,
            "type": record.spec.capability_type,
            "state": _capability_state_cell(record),
            "exposures": _capability_exposure_cell(record),
            "owner": record.spec.owner_ref or "-",
        }
        for record in capabilities
    ]
    print(table(rows, ["name", "identity", "type", "state", "exposures", "owner"]))


@app.command("seed")
def _seed(
    workspace_root: WorkspaceOpt = None,
    external_root: ExternalOpt = None,
    output_format: FormatOpt = "yaml",
) -> None:
    """print conservative registry seed records; never overwrites registry data."""
    workspace = expand(workspace_root or default_workspace_root())
    external = expand(external_root or default_external_root())
    assets = scan_inventory(workspace, external)
    if output_format == "yaml":
        print(render_seed_yaml(assets))
        return
    if output_format == "json":
        print(render_json(_SeedJson(seed_documents(assets))))
        return
    raise RegistrarError("--format must be yaml or json")


@worktree_app.command("create")
def _worktree_create(
    repo_path: Annotated[Path, typer.Argument(help="source repo path")],
    owner_ref: Annotated[
        str,
        typer.Option("--owner-ref", help="PM issue like PROJ-542 or none:<reason>"),
    ],
    slug: Annotated[
        str,
        typer.Option("--slug", help="optional suffix for generated branch/path"),
    ] = "",
    branch: Annotated[
        str,
        typer.Option("--branch", help="override generated branch name"),
    ] = "",
    path: Annotated[
        Path | None,
        typer.Option("--path", help="override generated worktree path"),
    ] = None,
    world: Annotated[
        str,
        typer.Option("--world", help="override inferred world: personal or work"),
    ] = "",
    source_repo: Annotated[
        str,
        typer.Option("--source-repo", help="override source repo label"),
    ] = "",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="print plan only; do not create or write"),
    ] = False,
    workspace_root: WorkspaceOpt = None,
    registry_root: RegistryOpt = None,
    output_format: WorktreeFormatOpt = "table",
) -> None:
    """create a git worktree and write its registrar owner record."""
    workspace = expand(workspace_root or default_workspace_root())
    root = _required_registry_root(registry_root)
    records = load_registry(root)
    plan = plan_create_worktree(
        repo_path,
        owner_ref,
        workspace,
        root,
        records,
        slug=slug,
        branch=branch,
        path=path,
        world=world,
        source_repo=source_repo,
    )
    if not dry_run:
        plan = apply_create_worktree(plan)
    _render_worktree_plan(plan, output_format)


@worktree_app.command("register")
def _worktree_register(
    worktree_path: Annotated[Path, typer.Argument(help="existing worktree path")],
    owner_ref: Annotated[
        str,
        typer.Option("--owner-ref", help="PM issue like PROJ-542 or none:<reason>"),
    ],
    world: Annotated[
        str,
        typer.Option("--world", help="override inferred world: personal or work"),
    ] = "",
    source_repo: Annotated[
        str,
        typer.Option("--source-repo", help="override source repo label"),
    ] = "",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="print plan only; do not write"),
    ] = False,
    workspace_root: WorkspaceOpt = None,
    registry_root: RegistryOpt = None,
    output_format: WorktreeFormatOpt = "table",
) -> None:
    """write a registrar owner record for an existing worktree."""
    workspace = expand(workspace_root or default_workspace_root())
    root = _required_registry_root(registry_root)
    records = load_registry(root)
    plan = plan_register_worktree(
        worktree_path,
        owner_ref,
        workspace,
        root,
        records,
        world=world,
        source_repo=source_repo,
    )
    if not dry_run:
        plan = apply_register_worktree(plan)
    _render_worktree_plan(plan, output_format)


@worktree_app.command("audit")
def _worktree_audit(
    asset: Annotated[
        str | None,
        typer.Argument(help="optional registry identity, unique short name, or path"),
    ] = None,
    owner_ref: Annotated[
        str,
        typer.Option("--owner-ref", help="filter by PM issue owner_ref"),
    ] = "",
    include_retired: Annotated[
        bool,
        typer.Option("--include-retired", help="include Tombstone worktree records"),
    ] = False,
    workspace_root: WorkspaceOpt = None,
    registry_root: RegistryOpt = None,
    output_format: FormatOpt = "table",
) -> None:
    """audit registered worktrees against docket owner and local git state."""
    workspace = expand(workspace_root or default_workspace_root())
    root = _required_registry_root(registry_root)
    records = load_registry(root)
    items = audit_worktrees(
        records,
        workspace,
        asset=asset,
        owner_ref=owner_ref,
        include_retired=include_retired,
    )
    _render_worktree_audit(items, output_format)


@worktree_app.command("owner")
def _worktree_owner(
    path: Annotated[Path, typer.Argument(help="worktree path or path inside it")],
    registry_root: RegistryOpt = None,
    output_format: FormatOpt = "table",
) -> None:
    """print the registrar owner_ref for a registered worktree path."""
    root = _required_registry_root(registry_root)
    records = load_registry(root)
    owner = resolve_worktree_owner(path, records)
    _render_worktree_owner(owner, output_format)


@worktree_app.command("closeout")
def _worktree_closeout(
    asset: Annotated[
        str,
        typer.Argument(help="registered worktree identity, unique short name, or path"),
    ],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="plan only; no changes")] = False,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="remove worktree and close the active record"),
    ] = False,
    owner_active_ok: Annotated[
        bool,
        typer.Option(
            "--owner-active-ok",
            help="allow closeout even when docket owner is not closed",
        ),
    ] = False,
    allow_unmerged: Annotated[
        bool,
        typer.Option("--allow-unmerged", help="preserve branch even if it is unmerged"),
    ] = False,
    stale_record: Annotated[
        bool,
        typer.Option(
            "--stale-record",
            help="allow removing an active record whose path is already missing",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="discard a dirty/untracked worktree (git worktree remove --force)",
        ),
    ] = False,
    delete_branch: Annotated[
        bool,
        typer.Option(
            "--delete-branch",
            help="also delete the local branch (default: preserve)",
        ),
    ] = False,
    record_mode: Annotated[
        str,
        typer.Option("--record", help="delete or tombstone; default delete"),
    ] = "delete",
    workspace_root: WorkspaceOpt = None,
    registry_root: RegistryOpt = None,
    output_format: FormatOpt = "table",
) -> None:
    """close a registered worktree while preserving its git branch by default."""
    if apply and dry_run:
        raise RegistrarError("pass either --dry-run or --apply, not both")
    if not apply and not dry_run:
        raise RegistrarError("worktree closeout requires --dry-run or --apply")
    workspace = expand(workspace_root or default_workspace_root())
    root = _required_registry_root(registry_root)
    records = load_registry(root)
    result = closeout_worktree(
        records,
        asset,
        workspace,
        apply=apply,
        owner_active_ok=owner_active_ok,
        allow_unmerged=allow_unmerged,
        stale_record=stale_record,
        force=force,
        delete_branch=delete_branch,
        record_mode=record_mode,  # type: ignore[arg-type]
    )
    _render_worktree_closeout(result, output_format)
    if result.blocked:
        raise RegistrarError(
            f"{result.name}: closeout blocked by {', '.join(result.blockers)}"
        )


# `remove` is a discoverability alias of `closeout`; it is the same lifecycle-gated
# teardown (docket owner check, branch-merge check, finalizers), not a raw delete.
worktree_app.command(
    "remove",
    help="alias of closeout: lifecycle-gated worktree teardown (docket-gated by default).",
)(_worktree_closeout)


@app.command("relocate")
def _relocate(
    asset: Annotated[
        str,
        typer.Argument(help="registry identity, unique short name, or absolute path"),
    ],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="plan only; no changes")] = False,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="execute the move and rewrite live refs"),
    ] = False,
    broad_sweep: Annotated[
        bool,
        typer.Option(
            "--broad-sweep",
            help="dry-run only: also report refs found by a full workspace sweep",
        ),
    ] = False,
    workspace_root: WorkspaceOpt = None,
    registry_root: RegistryOpt = None,
    output_format: FormatOpt = "table",
) -> None:
    """plan (or, with --apply, execute) placement correction for one asset."""
    if apply and dry_run:
        raise RegistrarError("pass either --dry-run or --apply, not both")
    if apply and broad_sweep:
        raise RegistrarError("--broad-sweep is dry-run only")
    if not apply and not dry_run:
        raise RegistrarError("relocate requires --dry-run or --apply")
    workspace = expand(workspace_root or default_workspace_root())
    root = _required_registry_root(registry_root)
    records = load_registry(root)
    if apply:
        result = apply_relocate(records, asset, workspace, root)
        _render_relocate_result(result, output_format)
        if not result.verified:
            raise RegistrarError(
                f"{result.name}: applied but NOT verified — "
                f"{len(result.remaining_refs)} live ref(s) still point at the old "
                "path; review before committing"
            )
        return
    plan = relocate_plan(records, asset, workspace)
    if broad_sweep:
        plan = replace(
            plan,
            workspace_sweep_refs=tuple(
                scan_workspace_sweep_refs(
                    plan.source_path,
                    workspace,
                    exclude_roots=(root,),
                )
            ),
        )
    if output_format == "json":
        print(render_json(plan))
        return
    if output_format != "table":
        raise RegistrarError("--format must be table or json")
    print(
        table(
            [
                {"field": "source", "value": str(plan.source_path)},
                {"field": "target", "value": str(plan.target_path)},
                {"field": "reason", "value": plan.reason},
                {"field": "affected_refs", "value": _refs_cell(plan.affected_refs)},
                {
                    "field": "workspace_sweep (review)",
                    "value": _refs_cell(plan.workspace_sweep_refs),
                },
                {"field": "rollback", "value": str(plan.rollback_path)},
                {"field": "finalizers", "value": ", ".join(plan.finalizers)},
            ],
            ["field", "value"],
        )
    )


def _render_relocate_result(result: RelocateResult, output_format: str) -> None:
    if output_format == "json":
        print(render_json(result))
        return
    if output_format != "table":
        raise RegistrarError("--format must be table or json")
    print(
        table(
            [
                {"field": "moved", "value": f"{result.source_path} -> {result.target_path}"},
                {"field": "rewritten", "value": _refs_cell(result.rewritten_refs)},
                {"field": "preserved (review)", "value": _refs_cell(result.preserved_refs)},
                {
                    "field": "registry_file",
                    "value": str(result.registry_file) if result.registry_file else "none",
                },
                {"field": "repos_to_commit", "value": "; ".join(result.repos_to_commit) or "none"},
                {"field": "launchd_reload", "value": "; ".join(result.launchd_reload) or "none"},
                {"field": "verified", "value": str(result.verified).lower()},
                {"field": "remaining_refs", "value": _refs_cell(result.remaining_refs)},
                {"field": "rollback", "value": " ; ".join(result.rollback)},
            ],
            ["field", "value"],
        )
    )


def _render_worktree_plan(plan: WorktreePlan, output_format: str) -> None:
    if output_format == "json":
        print(render_json(plan))
        return
    if output_format == "yaml":
        print(render_document_yaml(plan))
        return
    if output_format != "table":
        raise RegistrarError("--format must be table, json, or yaml")
    print(
        table(
            [
                {"field": "action", "value": plan.action},
                {"field": "applied", "value": str(plan.applied).lower()},
                {"field": "owner_ref", "value": plan.owner_ref},
                {"field": "world", "value": plan.world},
                {"field": "source_repo", "value": plan.source_repo},
                {"field": "worktree_path", "value": str(plan.worktree_path)},
                {"field": "branch", "value": plan.branch or "-"},
                {"field": "registry_file", "value": str(plan.registry_file)},
                {"field": "command", "value": " ".join(plan.command) or "-"},
            ],
            ["field", "value"],
        )
    )


def _render_worktree_audit(
    items: list[WorktreeAuditItem],
    output_format: str,
) -> None:
    if output_format == "json":
        print(render_json(items))
        return
    if output_format != "table":
        raise RegistrarError("--format must be table or json")
    rows = [
        {
            "name": item.name,
            "owner": item.owner_ref or "-",
            "owner_state": item.owner_state,
            "path": item.path_state,
            "branch": item.branch or "-",
            "branch_state": item.branch_state,
            "git": _git_cell(item.dirty, item.branch)
            + (f" +{item.untracked_count}?" if item.untracked_count else ""),
            "recommendation": item.recommendation,
        }
        for item in items
    ]
    print(
        table(
            rows,
            [
                "name",
                "owner",
                "owner_state",
                "path",
                "branch",
                "branch_state",
                "git",
                "recommendation",
            ],
        )
    )


def _render_worktree_owner(owner: WorktreeOwner, output_format: str) -> None:
    if output_format == "json":
        print(render_json(owner))
        return
    if output_format != "table":
        raise RegistrarError("--format must be table or json")
    print(
        table(
            [
                {"field": "found", "value": str(owner.found).lower()},
                {"field": "owner_ref", "value": owner.owner_ref or "-"},
                {"field": "path", "value": str(owner.path)},
                {
                    "field": "record_path",
                    "value": str(owner.record_path) if owner.record_path else "-",
                },
                {"field": "identity", "value": owner.identity or "-"},
                {"field": "lifecycle", "value": owner.lifecycle or "-"},
            ],
            ["field", "value"],
        )
    )


def _render_worktree_closeout(
    result: WorktreeCloseoutResult,
    output_format: str,
) -> None:
    if output_format == "json":
        print(render_json(result))
        return
    if output_format != "table":
        raise RegistrarError("--format must be table or json")
    print(
        table(
            [
                {"field": "name", "value": result.name},
                {"field": "path", "value": str(result.path)},
                {"field": "applied", "value": str(result.applied).lower()},
                {"field": "blocked", "value": str(result.blocked).lower()},
                {"field": "blockers", "value": ", ".join(result.blockers) or "-"},
                {"field": "actions", "value": ", ".join(result.actions)},
                {"field": "record", "value": result.record_mode},
                {
                    "field": "registry_file",
                    "value": str(result.source_file) if result.source_file else "-",
                },
            ],
            ["field", "value"],
        )
    )


def _refs_cell(refs: tuple[Ref, ...]) -> str:
    return (
        "; ".join(f"[{ref.category}] {ref.kind} {ref.source}" for ref in refs) or "none"
    )


@app.command("closeout")
def _closeout(
    asset: Annotated[
        str,
        typer.Argument(help="registry identity, unique short name, or path"),
    ],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="required in v0")] = False,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="not implemented in v0"),
    ] = False,
    workspace_root: WorkspaceOpt = None,
    registry_root: RegistryOpt = None,
    output_format: FormatOpt = "table",
) -> None:
    """plan closeout finalizers for one asset."""
    if apply:
        raise RegistrarError("closeout --apply is not implemented in v0")
    if not dry_run:
        raise RegistrarError("closeout requires --dry-run in v0")
    workspace = expand(workspace_root or default_workspace_root())
    records = load_registry(_registry_root(registry_root))
    plan = closeout_plan(records, asset, workspace)
    if output_format == "json":
        print(render_json(plan))
        return
    if output_format != "table":
        raise RegistrarError("--format must be table or json")
    print(
        table(
            [
                {"field": "path", "value": str(plan.path)},
                {"field": "blocked", "value": str(plan.blocked).lower()},
                {"field": "finalizers", "value": ", ".join(plan.finalizers)},
                {"field": "actions", "value": ", ".join(plan.actions)},
            ],
            ["field", "value"],
        )
    )


def _load_tiers() -> dict[str, dict[str, str]]:
    p = Path.home() / ".config/registrar/tiers.toml"
    if not p.exists():
        return {}
    import tomllib

    with open(p, "rb") as f:
        data = tomllib.load(f)
    result: dict[str, dict[str, str]] = {}
    tiers = data.get("tiers", {})
    if not isinstance(tiers, dict):
        print("error: tiers.toml must define a [tiers] table", file=sys.stderr)
        sys.exit(2)
    for name, cfg in tiers.items():
        if not isinstance(cfg, dict) or "workspace_root" not in cfg or "registry_root" not in cfg:
            print(
                f"error: tier '{name}' must define workspace_root and registry_root",
                file=sys.stderr,
            )
            sys.exit(2)
        result[name] = {
            "workspace_root": str(Path(cfg["workspace_root"]).expanduser()),
            "registry_root": str(Path(cfg["registry_root"]).expanduser()),
        }
    return result


def _consume_tier(argv: list[str]) -> list[str]:
    """Extract --tier <name> from argv, set env vars, return remaining argv."""
    if "--tier" not in argv:
        return argv
    idx = argv.index("--tier")
    if idx + 1 >= len(argv):
        print("error: --tier requires a value", file=sys.stderr)
        sys.exit(2)
    tier_name = argv[idx + 1]
    tiers = _load_tiers()
    if tier_name not in tiers:
        avail = (
            ", ".join(tiers)
            if tiers
            else "(none configured in ~/.config/registrar/tiers.toml)"
        )
        print(f"error: unknown tier '{tier_name}'. Available: {avail}", file=sys.stderr)
        sys.exit(2)
    cfg = tiers[tier_name]
    os.environ["REGISTRAR_WORKSPACE_ROOT"] = cfg["workspace_root"]
    os.environ["REGISTRAR_REGISTRY_ROOT"] = cfg["registry_root"]
    return argv[:idx] + argv[idx + 2:]


def run() -> None:
    check_update("registrar", "the-orrery/registrar")
    argv = _consume_tier(sys.argv[1:])
    sys.argv = [sys.argv[0], *argv]
    try:
        app()
    except RegistrarError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        raise SystemExit(1) from exc


def _registry_root(registry_root: Path | None) -> Path:
    if registry_root is not None:
        return expand(registry_root)
    if _GLOBAL_REGISTRY_ROOT is not None:
        return expand(_GLOBAL_REGISTRY_ROOT)
    return default_registry_root()


def _required_registry_root(registry_root: Path | None) -> Path:
    root = _registry_root(registry_root)
    return root


def _git_cell(dirty: bool, branch: str) -> str:
    if len(branch) > 32:
        branch = f"{branch[:29]}..."
    suffix = "*" if dirty else ""
    return f"{branch or '-'}{suffix}"


def _capability_state_cell(record: RegistryAsset) -> str:
    states = [exposure.state for exposure in record.spec.exposures]
    if not states:
        return "-"
    return ", ".join(dict.fromkeys(states))


def _capability_exposure_cell(record: RegistryAsset) -> str:
    return (
        "; ".join(
            f"{exposure.type}:{exposure.name}"
            + (f" -> {exposure.target}" if exposure.target else "")
            for exposure in record.spec.exposures
        )
        or "-"
    )


class _SeedJson:
    def __init__(self, docs: list[dict[str, object]]) -> None:
        self.docs = docs

    def to_dict(self) -> dict[str, object]:
        return {"items": self.docs}


class _CapabilityJson:
    def __init__(self, records: list[RegistryAsset]) -> None:
        self.records = records

    def to_dict(self) -> dict[str, object]:
        return {"items": [record.to_dict() for record in self.records]}


if __name__ == "__main__":
    run()
