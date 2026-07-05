from pathlib import Path

from registrar.refs import (
    path_variants,
    scan_affected_refs,
    scan_workspace_sweep_refs,
)


def test_path_variants_covers_all_forms(monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/home/example")
    repo = Path("/home/example/workspace/open-source/memex")
    workspace = Path("/home/example/workspace")
    variants = set(path_variants(repo, workspace))
    assert "/home/example/workspace/open-source/memex" in variants
    assert "open-source/memex" in variants
    assert "${HOME}/workspace/open-source/memex" in variants
    assert "~" + "/workspace/open-source/memex" in variants


def test_scan_finds_launchd_symlink_and_text(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    repo = workspace / "open-source" / "memex"
    repo.mkdir(parents=True)
    (repo / "wrapper").write_text("#!/bin/sh\n", encoding="utf-8")

    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "com.test.memex.plist").write_text(
        f"  <string>{repo}</string>\n", encoding="utf-8"
    )

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "memex-run").symlink_to(repo / "wrapper")

    opsdir = workspace / "control-plane"
    opsdir.mkdir(parents=True)
    (opsdir / "assets.json").write_text(
        '{"repos": ["open-source/memex"]}\n', encoding="utf-8"
    )

    refs = scan_affected_refs(repo, workspace, scan_roots=[agents, bindir, opsdir])
    by_kind = {ref.kind: ref for ref in refs}

    assert by_kind["launchd"].source.name == "com.test.memex.plist"
    assert by_kind["symlink"].source.name == "memex-run"
    assert by_kind["text"].source.name == "assets.json"

    duplicate_refs = scan_affected_refs(repo, workspace, scan_roots=[opsdir, opsdir])
    assert [ref.source for ref in duplicate_refs] == [opsdir / "assets.json"]


def test_scan_ignores_unrelated_refs(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    repo = workspace / "open-source" / "memex"
    repo.mkdir(parents=True)
    noise = tmp_path / "noise"
    noise.mkdir()
    (noise / "other.txt").write_text("references open-source/other\n", encoding="utf-8")
    (noise / "link").symlink_to(workspace / "open-source" / "elsewhere")
    assert scan_affected_refs(repo, workspace, scan_roots=[noise]) == []


def test_scan_finds_home_path_in_shell_default(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(home))
    workspace = (home / "workspace").resolve()
    repo = workspace / "proxy-route"
    repo.mkdir(parents=True)

    bindir = workspace / "control-plane" / "shared" / "bin"
    bindir.mkdir(parents=True)
    shim = bindir / "proxy-route"
    shim.write_text(
        'repo="${PROXY_ROUTE_REPO:-${HOME}/workspace/proxy-route}"\n',
        encoding="utf-8",
    )

    refs = scan_affected_refs(repo, workspace, scan_roots=[bindir])
    assert [ref.source for ref in refs] == [shim]
    assert refs[0].category == "rewrite"


def test_scan_does_not_match_relative_suffix_inside_absolute_alias_path(
    tmp_path: Path,
) -> None:
    home = tmp_path.resolve()
    workspace = home / "workspace"
    repo = workspace / "platform" / "sharedlib"
    repo.mkdir(parents=True)
    config_root = workspace / "host-config"
    config_root.mkdir(parents=True)

    config = config_root / "config.yaml"
    config.write_text(
        f"repo: {home}/ExampleProjects/platform/sharedlib\n",
        encoding="utf-8",
    )
    assert scan_affected_refs(repo, workspace, scan_roots=[config_root]) == []

    config.write_text("repo: platform/sharedlib\n", encoding="utf-8")
    refs = scan_affected_refs(repo, workspace, scan_roots=[config_root])
    assert [ref.source for ref in refs] == [config]


def test_workspace_sweep_reports_only_refs_outside_default_roots(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    repo = workspace / "foo"
    repo.mkdir(parents=True)

    default_root = workspace / "control-plane"
    default_root.mkdir(parents=True)
    default_ref = default_root / "assets.json"
    default_ref.write_text(f'{{"path": "{repo}"}}\n', encoding="utf-8")

    sibling = workspace / "other-tool"
    sibling.mkdir(parents=True)
    sibling_ref = sibling / "config.toml"
    sibling_ref.write_text(f'path = "{repo}"\n', encoding="utf-8")

    refs = scan_workspace_sweep_refs(repo, workspace)

    assert [ref.source for ref in refs] == [sibling_ref]
    assert refs[0].category == "rewrite"
