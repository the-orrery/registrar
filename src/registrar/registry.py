"""Registry data loader."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import yaml

from .errors import RegistrarError
from .model import (
    API_VERSION,
    CAPABILITY_KIND,
    TOMBSTONE_KIND,
    AssetSpec,
    CapabilityExposure,
    RegistryAsset,
)

REGISTRY_SUFFIXES = {".yaml", ".yml", ".json"}
CAPABILITY_STATES = {
    "active",
    "blocked",
    "deprecated",
    "deferred",
    "defer-rebuild",
    "declared-not-loaded",
}


def load_registry(root: Path | None) -> list[RegistryAsset]:
    if root is None:
        return []
    root = root.expanduser().resolve()
    if not root.exists():
        raise RegistrarError(f"registry root does not exist: {root}")

    assets: list[RegistryAsset] = []
    for file in _registry_files(root):
        data = _read_document(file)
        if data:
            assets.append(_asset_from_document(data, file))
    _validate_unique_identities(assets)
    _validate_aliases(assets)
    return assets


def by_name_or_path(
    records: Iterable[RegistryAsset],
    value: str,
) -> RegistryAsset | None:
    records = list(records)
    maybe_path = Path(value).expanduser()
    for record in records:
        if record.identity == value:
            return record
    if maybe_path.is_absolute():
        resolved = maybe_path.resolve()
        for record in records:
            if record.path is not None and record.path == resolved:
                return record
    alias_matches = [record for record in records if value in record.aliases]
    if len(alias_matches) == 1:
        return alias_matches[0]
    if len(alias_matches) > 1:
        candidates = ", ".join(
            f"{record.kind}:{record.identity}" for record in alias_matches
        )
        raise RegistrarError(
            f"{value}: ambiguous metadata.aliases; use metadata.identity ({candidates})"
        )
    matches = [record for record in records if record.name == value]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        candidates = ", ".join(f"{record.kind}:{record.identity}" for record in matches)
        raise RegistrarError(
            f"{value}: ambiguous metadata.name; use metadata.identity ({candidates})"
        )
    return None


def index_records(records: Iterable[RegistryAsset]) -> dict[str, RegistryAsset]:
    records = list(records)
    pathful_name_counts = Counter(
        record.name for record in records if record.path is not None
    )
    index: dict[str, RegistryAsset] = {}
    for record in records:
        index[record.identity] = record
        if record.path is not None and pathful_name_counts[record.name] == 1:
            index[record.name] = record
        if record.path is not None:
            index[str(record.path)] = record
    return index


def _registry_files(root: Path) -> list[Path]:
    return sorted(
        file
        for file in root.rglob("*")
        if file.is_file()
        and file.suffix.lower() in REGISTRY_SUFFIXES
        and ".git" not in file.parts
    )


def _read_document(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    loaded = yaml.safe_load(raw)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise RegistrarError(f"registry file must contain a mapping: {path}")
    return cast(dict[str, Any], loaded)


def _asset_from_document(data: dict[str, Any], source_file: Path) -> RegistryAsset:  # noqa: C901, PLR0912
    api_version = str(data.get("apiVersion", ""))
    if api_version != API_VERSION:
        raise RegistrarError(
            f"{source_file}: unsupported apiVersion {api_version!r}; expected {API_VERSION}"
        )
    metadata = _mapping(data, "metadata", source_file)
    spec_data = _mapping(data, "spec", source_file)
    kind = str(data.get("kind", "")).strip() or "Repo"
    name = str(metadata.get("name", "")).strip()
    raw_identity = str(metadata.get("identity", "")).strip()
    raw_path = str(metadata.get("path", "")).strip()
    aliases = _string_list(metadata.get("aliases", []), "metadata.aliases", source_file)
    if not name:
        raise RegistrarError(f"{source_file}: metadata.name is required")
    if kind == CAPABILITY_KIND and raw_path:
        raise RegistrarError(
            f"{source_file}: metadata.path is not allowed for Capability; "
            "use spec.implementation_refs for implementation pointers"
        )
    if kind == TOMBSTONE_KIND and raw_path:
        raise RegistrarError(
            f"{source_file}: metadata.path is not allowed for Tombstone; "
            "use metadata.labels.old_path for historical location"
        )
    if kind not in {CAPABILITY_KIND, TOMBSTONE_KIND} and not raw_path:
        raise RegistrarError(f"{source_file}: metadata.path is required")
    path = Path(raw_path).expanduser().resolve() if raw_path else None

    labels = metadata.get("labels", {})
    if labels is None:
        labels = {}
    if not isinstance(labels, dict):
        raise RegistrarError(f"{source_file}: metadata.labels must be a mapping")

    allowed_actions = spec_data.get("allowed_actions", [])
    if allowed_actions is None:
        allowed_actions = []
    if not isinstance(allowed_actions, list):
        raise RegistrarError(f"{source_file}: spec.allowed_actions must be a list")

    implementation_refs = spec_data.get("implementation_refs", [])
    if implementation_refs is None:
        implementation_refs = []
    if not isinstance(implementation_refs, list):
        raise RegistrarError(f"{source_file}: spec.implementation_refs must be a list")

    capability_type = str(spec_data.get("capability_type", "")).strip()
    exposures = _capability_exposures(spec_data, source_file)
    if kind == CAPABILITY_KIND:
        if not capability_type:
            raise RegistrarError(f"{source_file}: spec.capability_type is required")
        if not exposures:
            raise RegistrarError(f"{source_file}: spec.exposures must not be empty")

    finalizers = data.get("finalizers", [])
    if finalizers is None:
        finalizers = []
    if not isinstance(finalizers, list):
        raise RegistrarError(f"{source_file}: finalizers must be a list")

    placement = str(spec_data.get("placement", "")).strip()
    spec = AssetSpec(
        owner_ref=str(spec_data.get("owner_ref", "")).strip(),
        lifecycle=str(spec_data.get("lifecycle", "")).strip(),
        placement=placement,
        restore_policy=str(spec_data.get("restore_policy", "")).strip(),
        allowed_actions=tuple(str(item) for item in allowed_actions),
        closeout_policy=str(spec_data.get("closeout_policy", "")).strip(),
        capability_type=capability_type,
        exposures=tuple(exposures),
        implementation_refs=tuple(str(item) for item in implementation_refs),
        owner_uid=str(spec_data.get("owner_uid", "")).strip(),
    )
    return RegistryAsset(
        kind=kind,
        identity=raw_identity
        or derive_identity(kind, name, placement, capability_type),
        name=name,
        path=path,
        aliases=tuple(aliases),
        labels={str(key): str(value) for key, value in labels.items()},
        spec=spec,
        finalizers=tuple(str(item) for item in finalizers),
        source_file=source_file,
    )


def _mapping(data: dict[str, Any], key: str, source_file: Path) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise RegistrarError(f"{source_file}: {key} mapping is required")
    return cast(dict[str, Any], value)


def _string_list(raw: Any, field: str, source_file: Path) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RegistrarError(f"{source_file}: {field} must be a list")
    values: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str):
            raise RegistrarError(f"{source_file}: {field}[{index}] must be a string")
        value = item.strip()
        if not value:
            raise RegistrarError(f"{source_file}: {field}[{index}] must not be empty")
        values.append(value)
    return values


def _capability_exposures(
    spec_data: dict[str, Any],
    source_file: Path,
) -> list[CapabilityExposure]:
    raw = spec_data.get("exposures", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise RegistrarError(f"{source_file}: spec.exposures must be a list")

    exposures: list[CapabilityExposure] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise RegistrarError(
                f"{source_file}: spec.exposures[{index}] must be a mapping"
            )
        exposure_type = str(item.get("type", "")).strip()
        name = str(item.get("name", "")).strip()
        state = str(item.get("state", "active")).strip() or "active"
        if not exposure_type:
            raise RegistrarError(
                f"{source_file}: spec.exposures[{index}].type is required"
            )
        if not name:
            raise RegistrarError(
                f"{source_file}: spec.exposures[{index}].name is required"
            )
        if state not in CAPABILITY_STATES:
            raise RegistrarError(
                f"{source_file}: spec.exposures[{index}].state {state!r} "
                f"is not supported"
            )
        exposures.append(
            CapabilityExposure(
                type=exposure_type,
                name=name,
                target=str(item.get("target", "")).strip(),
                state=state,
                policy=str(item.get("policy", "")).strip(),
            )
        )
    return exposures


def derive_identity(
    kind: str,
    name: str,
    placement: str,
    capability_type: str,
) -> str:
    if placement == "workspace/root":
        return f"workspace/root/{name}"
    if placement.startswith("workspace/"):
        return f"{placement.removeprefix('workspace/')}/{name}"
    if placement:
        return f"{placement}/{name}"
    if kind == CAPABILITY_KIND:
        return f"capabilities/{capability_type or 'unknown'}/{name}"
    return f"{kind.lower()}/{name}"


def _validate_unique_identities(records: list[RegistryAsset]) -> None:
    seen: dict[str, Path | None] = {}
    for record in records:
        if record.identity in seen:
            first = seen[record.identity]
            locations = (
                f"{first} and {record.source_file}"
                if first is not None
                else str(record.source_file)
            )
            raise RegistrarError(
                f"duplicate metadata.identity {record.identity!r} in registry records: "
                f"{locations}"
            )
        seen[record.identity] = record.source_file


def _validate_aliases(records: list[RegistryAsset]) -> None:
    identities = {record.identity: record.source_file for record in records}
    names = {record.name for record in records}
    seen: dict[str, Path | None] = {}
    for record in records:
        record_seen: set[str] = set()
        for alias in record.aliases:
            if alias in record_seen:
                raise RegistrarError(
                    f"{record.source_file}: duplicate metadata.aliases value {alias!r}"
                )
            record_seen.add(alias)
            if alias in identities:
                raise RegistrarError(
                    f"{record.source_file}: metadata.aliases value {alias!r} "
                    "collides with metadata.identity"
                )
            if alias in names:
                raise RegistrarError(
                    f"{record.source_file}: metadata.aliases value {alias!r} "
                    "collides with metadata.name"
                )
            if alias in seen:
                first = seen[alias]
                locations = (
                    f"{first} and {record.source_file}"
                    if first is not None
                    else str(record.source_file)
                )
                raise RegistrarError(
                    f"duplicate metadata.aliases value {alias!r} in registry "
                    f"records: {locations}"
                )
            seen[alias] = record.source_file
