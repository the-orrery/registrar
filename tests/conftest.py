from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_docket_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DOCKET_ROOT", raising=False)
    monkeypatch.delenv("REGISTRAR_ACTIVE_TIER", raising=False)
    monkeypatch.delenv("REGISTRAR_PERSONAL_ROOT", raising=False)
    monkeypatch.delenv("REGISTRAR_WORK_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
