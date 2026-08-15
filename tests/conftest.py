"""Shared pytest fixtures and environment guardrails for bare-metal runs.

Bare-metal runs share the host filesystem and environment with production
tooling.  These fixtures guarantee tests never touch real documentation,
real index databases, or real listening ports:

- ``WYWY_DOCS_DIR``/``WYWY_ROOT``/``PORT`` are removed from the process
  environment for the whole session, so a subprocess server can never be
  redirected at production state by the ambient shell environment.
- ``wywy_docs.server._ROOT_DIR`` is pointed at a fresh throwaway directory
  before and after every test, so a direct-import test can never resolve
  root-dependent paths against the repo's real ``wywy_docs/`` directory.
"""

from __future__ import annotations

import os
import tempfile
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

#: Environment variables that can redirect the server at production state.
_SERVER_ENV_VARS = ("WYWY_DOCS_DIR", "WYWY_ROOT", "PORT")


@pytest.fixture(autouse=True, scope="session")
def _sanitized_server_env() -> Generator[None, None, None]:
    """Remove server-redirecting env vars for the whole test session."""
    saved: dict[str, str | None] = {
        var: os.environ.get(var) for var in _SERVER_ENV_VARS
    }
    for var in _SERVER_ENV_VARS:
        os.environ.pop(var, None)
    yield
    for var, value in saved.items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value


@pytest.fixture(autouse=True)
def _isolated_server_root() -> Generator[str, None, None]:
    """Point ``wywy_docs.server._ROOT_DIR`` at a throwaway directory.

    The module default ``""`` resolves to the process cwd, which on a bare
    metal host is the real repository; any root-dependent call made without
    an explicit root would then read or write the production database.  Each
    test gets a fresh temporary root instead, and the previous value is
    restored afterwards so no test leaks its root into the next one.
    """
    import wywy_docs.server as server_mod

    previous = server_mod._ROOT_DIR
    with tempfile.TemporaryDirectory() as tmp:
        # The module uses a global as its root; tests deliberately share it.
        server_mod._ROOT_DIR = tmp
        yield tmp
        server_mod._ROOT_DIR = previous
