"""Conservative registry seed generation."""

from __future__ import annotations

from typing import Any

import yaml

from .model import API_VERSION, InventoryAsset
from .registry import derive_identity


def seed_documents(assets: list[InventoryAsset]) -> list[dict[str, Any]]:
    return [_seed_document(asset) for asset in assets]


def render_seed_yaml(assets: list[InventoryAsset]) -> str:
    docs = seed_documents(assets)
    rendered = [
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False).strip() for doc in docs
    ]
    return "\n---\n".join(rendered)


def _seed_document(asset: InventoryAsset) -> dict[str, Any]:
    return {
        "apiVersion": API_VERSION,
        "kind": asset.kind,
        "metadata": {
            "identity": derive_identity(
                asset.kind,
                asset.name,
                asset.current_placement,
                "",
            ),
            "name": asset.name,
            "path": str(asset.path),
            "labels": asset.labels,
        },
        "spec": {
            "owner_ref": "unknown:seeded",
            "lifecycle": "unknown",
            "placement": asset.current_placement,
            "restore_policy": "unknown",
            "allowed_actions": [
                "inspect",
                "relocate-dry-run",
                "closeout-dry-run",
            ],
        },
        "finalizers": [
            "pm-owner-required",
            "closeout-recorded",
        ],
    }
