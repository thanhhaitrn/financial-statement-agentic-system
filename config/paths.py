"""Where AgentFinX keeps the data it writes.

Two roots, both under ``FINX_HOME``:

- ``storage/``: data people depend on: accounts and conversations, report
  datasets, imported reports, the news cache. Back it up; never delete it to
  clean up.
- ``artifacts/``: what runs produce: evaluation reports, logs. Safe to delete.

``FINX_HOME`` defaults to the source checkout this code runs from (recognised
by its tracked ``storage/.gitignore``); the current directory never matters.
An installed wheel has no checkout, so it must be given ``FINX_HOME`` rather
than quietly start on a new, empty database somewhere else.

Paths are resolved on every call, so tests and tools can point ``FINX_HOME``
elsewhere at any time. Paths stored in the registry and manifests are
relative to ``FINX_HOME`` (``to_stored``/``from_stored``), so the whole
directory can move.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

HOME_ENV = "FINX_HOME"
WEB_DB_ENV = "FINX_WEB_DB"
# The layout before storage/ existed: everything under <checkout>/dataset_store.
LEGACY_STORE_NAME = "dataset_store"
# Written by ``agentfinx storage migrate --apply`` once storage/ is verified.
MIGRATION_RECORD_NAME = "migration.json"

_CHECKOUT_ROOT = Path(__file__).resolve().parents[1]
_verifying_migration = False


class StorageError(RuntimeError):
    """Storage is not configured, or its data is still in the old layout."""


def checkout_root() -> Optional[Path]:
    """The source checkout this code runs from, or None for an installed wheel."""

    return _CHECKOUT_ROOT if (_CHECKOUT_ROOT / "storage" / ".gitignore").is_file() else None


def home() -> Path:
    raw = os.environ.get(HOME_ENV, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    root = checkout_root()
    if root is None:
        raise StorageError(
            f"{HOME_ENV} is not set and this is not a source checkout: set {HOME_ENV} to the "
            "directory that holds storage/ and artifacts/."
        )
    return root


def storage_root(root: Optional[Path] = None) -> Path:
    """``storage/`` without the layout check (for the migration itself)."""

    return (root or home()) / "storage"


def storage_dir() -> Path:
    """``storage/``, refusing while the data is still in the old layout.

    New code must never start next to an unmigrated ``dataset_store/``: it
    would create an empty registry and an empty account database and run on
    them as if they were the real ones.
    """

    root = home()
    legacy = root / LEGACY_STORE_NAME
    migrated = (storage_root(root) / MIGRATION_RECORD_NAME).is_file()
    if legacy.is_dir() and not migrated and not _verifying_migration:
        raise StorageError(
            f"the data is still in the old layout ({legacy}); move it with "
            "`agentfinx storage migrate` (dry run), then `agentfinx storage migrate --apply`."
        )
    return storage_root(root)


@contextmanager
def verifying_migration() -> Iterator[None]:
    """For ``agentfinx storage migrate`` only: let the real code read the new
    storage/ before the migration is recorded, to verify it."""

    global _verifying_migration
    previous, _verifying_migration = _verifying_migration, True
    try:
        yield
    finally:
        _verifying_migration = previous


def datasets_dir() -> Path:
    return storage_dir() / "datasets"


def locks_dir() -> Path:
    return storage_dir() / "locks"


def default_web_db_path() -> Path:
    return storage_dir() / "web" / "finx_web.sqlite3"


def acquisition_dir() -> Path:
    return storage_dir() / "acquisition"


def news_cache_path() -> Path:
    return storage_dir() / "news" / "evidence.db"


def symbol_catalog_path() -> Path:
    return storage_dir() / "catalog" / "cafef_symbols.json"


def reports_dir() -> Path:
    """Report files people add by hand (``agentfinx ask --file``)."""

    return storage_dir() / "reports"


def artifacts_dir() -> Path:
    return home() / "artifacts"


def eval_runs_dir() -> Path:
    return artifacts_dir() / "eval"


def to_stored(path: str | Path) -> str:
    """How a path is written to the registry: relative to FINX_HOME when inside it."""

    resolved = Path(path).expanduser().resolve(strict=False)
    try:
        return resolved.relative_to(home()).as_posix()
    except ValueError:
        return str(resolved)


def from_stored(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else home() / path
