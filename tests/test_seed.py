from pathlib import Path

from registrar.inventory import scan_inventory
from registrar.seed import seed_documents


def test_seed_uses_conservative_unknown_owner(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "open-source" / "foo").mkdir(parents=True)

    assets = scan_inventory(workspace, external_root=tmp_path / "external")
    docs = seed_documents(assets)
    foo = next(doc for doc in docs if doc["metadata"]["name"] == "foo")

    assert foo["apiVersion"] == "registrar.local/v1alpha1"
    assert foo["metadata"]["identity"] == "open-source/foo"
    assert foo["spec"]["owner_ref"] == "unknown:seeded"
    assert foo["spec"]["lifecycle"] == "unknown"
    assert foo["spec"]["placement"] == "workspace/open-source"
