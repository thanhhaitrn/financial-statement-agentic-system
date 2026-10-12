"""Every test runs against its own empty FINX_HOME.

Storage paths come from FINX_HOME (config.paths), so a test that forgets to
relocate storage still writes to a temporary directory, never to the
developer's accounts, reports or news cache. A test that needs files at known
places sets FINX_HOME itself, usually to its ``tmp_path``.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_finx_home(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("FINX_HOME", str(tmp_path_factory.mktemp("finx_home")))
    monkeypatch.delenv("FINX_WEB_DB", raising=False)
