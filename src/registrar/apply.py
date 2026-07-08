"""Execute a relocate plan: rewrite live refs, move the asset, verify.

``relocate --dry-run`` reports the plan; this module is what ``--apply`` runs.
It automates the mechanical, safe part of a workspace move — symlink/shim
re-pointing, config-file path edits, the registry record update, and the
physical directory move — then re-scans to prove no live ref still points at the
old path.

Safety properties:

- **Transactional.** Reversible ref edits happen first; the directory move is
  last. Every mutated file/symlink is snapshotted; any failure rolls everything
  back and re-raises, so a crash never leaves a half-migrated system.
- **Bounded, honest verification.** ``verified`` means "no live functional ref
  to the old path survives *in the scanned roots*, and doctor agrees" — it is
  not a global guarantee. Use ``relocate --dry-run --broad-sweep`` to surface
  full-workspace review-only refs before risky moves. The CLI exits non-zero
  when verification fails.
- **Human-in-the-loop for judgement.** It never auto-commits, never auto-reloads
  launchd, and never rewrites ``preserve`` content (PM data, archives, runtime
  records, ``.md`` docs): those surface as a review list / reviewable diffs.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path

import yaml

from .errors import RegistrarError
from .model import Ref, RegistryAsset, RelocateResult
from .paths import current_placement
from .plan import relocate_plan
from .refs import path_ref_regex, scan_affected_refs
from .registry import load_registry


def apply_relocate(
    records: list[RegistryAsset],
    asset: str,
    workspace_root: Path,
    registry_root: Path,
) -> RelocateResult:
    plan = relocate_plan(records, asset, workspace_root)
    source = plan.source_path
    target = plan.target_path
    _check_preconditions(plan.name, source, target)

    rewrite_refs = [ref for ref in plan.affected_refs if ref.category == "rewrite"]
    preserve_refs = [ref for ref in plan.affected_refs if ref.category != "rewrite"]
    pairs = _rewrite_pairs(source, target, workspace_root)

    text_backups: dict[Path, str] = {}
    symlink_backups: dict[Path, str] = {}
    rewritten: list[Ref] = []
    touched: list[Path] = []
    launchd_reload: list[str] = []
    registry_file: Path | None = None
    moved = False
    try:
        # 1. Rewrite live functional refs (all reversible) BEFORE the move.
        for ref in rewrite_refs:
            if ref.kind == "symlink":
                if not _rewrite_symlink(ref.source, source, target, symlink_backups):
                    continue
            elif not _rewrite_text(ref.source, pairs, text_backups):
                continue
            rewritten.append(ref)
            touched.append(ref.source)
            if ref.kind == "launchd":
                launchd_reload.append(str(ref.source))

        # 2. Point the registry record at the new path.
        registry_file = _update_registry_record(
            records, plan.identity, target, text_backups
        )
        if registry_file is not None:
            touched.append(registry_file)

        # 3. Move the directory last — the single hard-to-reverse step.
        target.parent.mkdir(parents=True, exist_ok=True)
        _move(source, target)
        moved = True
    except Exception as exc:
        _rollback(text_backups, symlink_backups, source, target, moved=moved)
        raise RegistrarError(
            f"{plan.name}: apply failed and was rolled back ({exc})"
        ) from exc

    # 4. Verify (bounded by scan roots): no live functional ref to the old path,
    #    and the reloaded record sits at the intended placement. Matched by PATH,
    #    not name — a same-named asset in another class (e.g. an external-readonly
    #    clone) must not satisfy or fool this check.
    remaining = [
        ref
        for ref in scan_affected_refs(source, workspace_root)
        if ref.category == "rewrite"
    ]
    reloaded = load_registry(registry_root)
    record = next(
        (r for r in reloaded if r.identity == plan.identity and r.path == target), None
    )
    placement_ok = (
        record is not None
        and record.path is not None
        and target.exists()
        and current_placement(record.path, workspace_root) == record.spec.placement
    )
    verified = not remaining and placement_ok

    repos = _repos_to_commit(touched)
    rollback = _rollback_steps(source, target, repos)

    return RelocateResult(
        identity=plan.identity,
        name=plan.name,
        source_path=source,
        target_path=target,
        moved=True,
        rewritten_refs=tuple(rewritten),
        preserved_refs=tuple(preserve_refs),
        registry_file=registry_file,
        repos_to_commit=tuple(repos),
        launchd_reload=tuple(launchd_reload),
        verified=verified,
        remaining_refs=tuple(remaining),
        rollback=tuple(rollback),
    )


def _check_preconditions(name: str, source: Path, target: Path) -> None:
    if source == target:
        raise RegistrarError(
            f"{name}: already at target placement; nothing to relocate"
        )
    if not source.exists():
        raise RegistrarError(f"source path does not exist: {source}")
    if not source.is_dir():
        raise RegistrarError(f"source is not a directory: {source}")
    if target.exists():
        raise RegistrarError(f"target already exists, refusing to overwrite: {target}")
    if _is_relative_to(target, source):
        raise RegistrarError(f"target {target} is inside source {source}")


def _move(source: Path, target: Path) -> None:
    try:
        os.rename(source, target)
    except OSError:
        shutil.move(str(source), str(target))


def _rewrite_pairs(
    source: Path, target: Path, workspace_root: Path
) -> list[tuple[str, str]]:
    """(old, new) string pairs covering every textual form a ref may use.

    Ordered absolute first, workspace-relative last. Each pair is applied with a
    path-boundary-aware regex (see ``path_ref_regex``), so neither prefix
    collisions nor a stray fragment from an earlier pair can corrupt a sibling.
    """
    s, t = str(source), str(target)
    pairs = [(s, t)]
    home = str(_home())
    if s.startswith(home + os.sep) and t.startswith(home + os.sep):
        s_rel, t_rel = s[len(home):], t[len(home):]
        for prefix in ("${HOME}", "$HOME", "~"):
            pairs.append((prefix + s_rel, prefix + t_rel))
    ws = str(workspace_root)
    if s.startswith(ws + os.sep) and t.startswith(ws + os.sep):
        s_rel = s[len(ws) + 1:]
        # Mirror path_variants: never rewrite a bare leaf token.
        if os.sep in s_rel:
            pairs.append((s_rel, t[len(ws) + 1:]))
    return pairs


def _apply_pairs(text: str, pairs: list[tuple[str, str]]) -> str:
    for old, new in pairs:
        text = path_ref_regex(old).sub(lambda _match, _new=new: _new, text)
    return text


def _rewrite_text(
    path: Path, pairs: list[tuple[str, str]], backups: dict[Path, str]
) -> bool:
    text = path.read_text(encoding="utf-8")
    updated = _apply_pairs(text, pairs)
    if updated == text:
        return False
    backups[path] = text
    path.write_text(updated, encoding="utf-8")
    return True


def _rewrite_symlink(
    link: Path, source: Path, target: Path, backups: dict[Path, str]
) -> bool:
    raw = os.readlink(link)
    old_target = Path(raw) if os.path.isabs(raw) else link.parent / raw
    old_target = Path(os.path.normpath(old_target))
    try:
        inner = old_target.relative_to(source)
    except ValueError:
        return False
    backups[link] = raw  # record before unlinking, so rollback can restore it
    new_target = target / inner
    link.unlink()
    link.symlink_to(new_target)
    return True


def _update_registry_record(
    records: list[RegistryAsset],
    identity: str,
    target: Path,
    backups: dict[Path, str],
) -> Path | None:
    """Set ``metadata.path`` to the new location by parsing the YAML.

    Parsing (vs string-replace) is robust to whatever form the record stores the
    path in — absolute, ``~``-relative, or a form that differs from the resolved
    path — so the one record we are authoritative over is always updated.
    """
    for record in records:
        if record.identity == identity and record.source_file is not None:
            original = record.source_file.read_text(encoding="utf-8")
            data = yaml.safe_load(original)
            if not isinstance(data, dict) or not isinstance(data.get("metadata"), dict):
                raise RegistrarError(
                    f"registry record {record.source_file} has no metadata mapping"
                )
            backups[record.source_file] = original
            data["metadata"]["path"] = str(target)
            record.source_file.write_text(
                yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            return record.source_file
    return None


def _rollback(
    text_backups: dict[Path, str],
    symlink_backups: dict[Path, str],
    source: Path,
    target: Path,
    *,
    moved: bool,
) -> None:
    if moved:
        with contextlib.suppress(OSError):
            _move(target, source)
    for path, content in text_backups.items():
        with contextlib.suppress(OSError):
            path.write_text(content, encoding="utf-8")
    for link, raw in symlink_backups.items():
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(raw)
        except OSError:
            pass


def _repos_to_commit(paths: list[Path]) -> list[str]:
    roots: set[str] = set()
    for path in paths:
        root = _git_root(path)
        if root is not None:
            roots.add(str(root))
    return sorted(roots)


def _git_root(path: Path) -> Path | None:
    start = path if path.is_dir() else path.parent
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _rollback_steps(source: Path, target: Path, repos: list[str]) -> list[str]:
    steps = [f"mv {target} {source}"]
    steps.extend(
        f"git -C {repo} checkout -- .  # discard ref/registry edits" for repo in repos
    )
    return steps


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())
