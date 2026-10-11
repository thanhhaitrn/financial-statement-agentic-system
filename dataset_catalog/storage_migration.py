"""``agentfinx storage migrate`` and ``agentfinx storage status``.

The old layout kept everything in ``<checkout>/dataset_store`` and wrote
absolute paths into the registry and manifests. ``migrate`` copies it into
the ``storage/`` layout of ``config.paths``:

    dataset_store/registry.json, registry_backups,
      manifests, raw_tables, sqlite, sources    -> storage/datasets/
    dataset_store/acquisition                   -> storage/acquisition/
    dataset_store/web/finx_web.sqlite3          -> storage/web/
    dataset_store/web/evidence.db               -> storage/news/
    dataset_store/web/cafef_symbols.json*       -> storage/catalog/
    report files datasets were built from       -> storage/reports/
    anything else in dataset_store              -> storage/legacy/

and rewrites the stored paths relative to FINX_HOME.

Safety:

- it copies and never deletes: dataset_store/ (and data/) stay as they were,
  to be removed by hand once the new layout is confirmed;
- it refuses while any lock in dataset_store/locks is held (a worker, a
  build, a registry write) or a worker reported in the last minute, and
  holds every lock while it copies;
- SQLite files are copied with the online backup API and checked
  (integrity_check, same tables and row counts); other files by SHA-256;
- the result is checked with the code the app runs: the registry loads
  without needing repair, and every ready dataset passes the check a chat
  question makes (a failure there would turn its conversations read-only);
- the old databases must not have changed while it copied (a running
  ``agentfinx serve`` would lose those writes);
- only then is storage/migration.json written. Until it exists the app
  refuses to start, so a failed or partial copy is never used.

Without ``--apply`` it prints the plan and changes nothing.
"""
# Code note: Dataset modules manage local registry records and derived artifact paths.

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator, Optional

from config import paths

SQLITE_MAGIC = b"SQLite format 3\x00"
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
_IGNORED_NAMES = {".DS_Store"}
_PATH_FIELDS = ("file_path", "source_pdf_path", "sqlite_db_path", "manifest_path", "raw_tables_path")
# A worker that reported this recently may still be running.
_WORKER_GRACE_SECONDS = 60

# Old place (relative to dataset_store/) -> new place (relative to storage/).
# The first entry whose path parts prefix the file wins; None: not copied.
LAYOUT: tuple[tuple[str, Optional[str]], ...] = (
    ("registry.json", "datasets/registry.json"),
    ("registry_backups", "datasets/registry_backups"),
    ("manifests", "datasets/manifests"),
    ("raw_tables", "datasets/raw_tables"),
    ("sqlite", "datasets/sqlite"),
    ("sources", "datasets/sources"),
    ("acquisition", "acquisition"),
    ("web/finx_web.sqlite3", "web/finx_web.sqlite3"),
    ("web/evidence.db", "news/evidence.db"),
    ("web/cafef_symbols.json", "catalog/cafef_symbols.json"),
    ("web/cafef_symbols.json.last-attempt", "catalog/cafef_symbols.json.last-attempt"),
    ("web/cafef_symbols.json.lock", None),
    ("locks", None),  # runtime only; storage/locks starts empty
)


class MigrationError(RuntimeError):
    """The migration cannot run, or its result failed a check."""


@dataclass
class Step:
    source: Path
    target: Path
    kind: str  # "registry" | "manifest" | "sqlite" | "file" | "report"


@dataclass
class Plan:
    home: Path
    legacy: Path
    storage: Path
    steps: list[Step] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reports: dict[Path, Path] = field(default_factory=dict)


def new_relative(rel: PurePosixPath) -> Optional[str]:
    for old, new in LAYOUT:
        old_parts = tuple(old.split("/"))
        if rel.parts[: len(old_parts)] == old_parts:
            if new is None:
                return None
            return "/".join([new, *rel.parts[len(old_parts):]])
    return "/".join(["legacy", *rel.parts])


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve(value: str, home: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else home / path).resolve(strict=False)


def _relocate(value: str, plan: Plan) -> str:
    """Where a stored path points after the migration, in stored form."""

    if not value:
        return value
    path = _resolve(value, plan.home)
    if path in plan.reports:
        return paths.to_stored(plan.reports[path])
    try:
        rel = path.relative_to(plan.legacy.resolve())
    except ValueError:
        return paths.to_stored(path)  # outside dataset_store: it stays where it is
    new = new_relative(PurePosixPath(rel.as_posix()))
    if new is None:
        raise MigrationError(f"a stored path points at runtime-only data: {value}")
    return paths.to_stored(plan.storage / new)


def _read_registry(legacy: Path) -> dict:
    path = legacy / "registry.json"
    if not path.is_file():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    from dataset_catalog.registry import REGISTRY_SCHEMA_VERSION

    if not isinstance(document, dict) or document.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise MigrationError(
            "dataset_store/registry.json is not a current (v2) registry; open it once with the "
            "previous version of the code so it upgrades itself, then migrate"
        )
    return document


def build_plan(home: Path) -> Plan:
    legacy = home / paths.LEGACY_STORE_NAME
    plan = Plan(home=home, legacy=legacy, storage=paths.storage_root(home))
    registry_document = _read_registry(legacy)

    # Report files datasets were built from, outside dataset_store (data/*.md).
    reports_dir = plan.storage / "reports"
    for record in registry_document.get("records", []):
        if record.get("managed_source") or not record.get("file_path"):
            continue
        source = _resolve(record["file_path"], home)
        if source in plan.reports or source.is_relative_to(plan.storage.resolve()):
            continue
        if not source.is_file():
            plan.warnings.append(
                f"dataset {record.get('dataset_id')}: report file {source} is missing; the path is "
                "kept, the dataset still answers, but cannot be rebuilt until the file is back"
            )
            continue
        target = reports_dir / source.name
        if target in plan.reports.values():
            raise MigrationError(f"two report files are both named {source.name}; rename one first")
        plan.reports[source] = target
        plan.steps.append(Step(source, target, "report"))

    for source in sorted(p for p in legacy.rglob("*") if p.is_file()):
        rel = PurePosixPath(source.relative_to(legacy).as_posix())
        if source.name in _IGNORED_NAMES:
            continue
        if source.name.endswith(_SQLITE_SIDECARS) and _is_sqlite(
                source.with_name(source.name.rsplit("-", 1)[0])):
            continue  # the backup API copies what the WAL holds
        new = new_relative(rel)
        if new is None:
            plan.skipped.append(str(rel))
            continue
        if new.startswith("legacy/"):
            plan.unknown.append(str(rel))
        target = plan.storage / new
        if rel == PurePosixPath("registry.json"):
            kind = "registry"
        elif rel.parts[0] == "manifests" and source.suffix == ".json":
            kind = "manifest"
        elif _is_sqlite(source):
            kind = "sqlite"
        else:
            kind = "file"
        plan.steps.append(Step(source, target, kind))
    return plan


def _held_locks(legacy: Path) -> list[str]:
    """Lock files another process holds right now."""

    return [path.name for path in sorted((legacy / "locks").glob("*.lock")) if not _try_lock(path)]


def _try_lock(path: Path, stack: Optional[ExitStack] = None) -> bool:
    """Take ``path`` without waiting; with ``stack``, keep it until the stack closes."""

    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    if stack is None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    else:
        stack.callback(os.close, fd)
        stack.callback(fcntl.flock, fd, fcntl.LOCK_UN)
    return True


@contextmanager
def _hold_legacy_locks(legacy: Path) -> Iterator[None]:
    with ExitStack() as stack:
        busy = [path.name for path in sorted((legacy / "locks").glob("*.lock")) if not _try_lock(path, stack)]
        if busy:
            raise MigrationError(
                f"in use: {', '.join(busy)}; stop `agentfinx serve`, the workers and any build first"
            )
        yield


def _recent_workers(web_db: Path) -> list[str]:
    if not web_db.is_file():
        return []
    conn = sqlite3.connect(web_db.as_uri() + "?mode=ro", uri=True)
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'worker_heartbeats'").fetchone()
        rows = conn.execute("SELECT queue, heartbeat_at FROM worker_heartbeats").fetchall() if has_table else []
    finally:
        conn.close()
    now = datetime.now(timezone.utc)
    recent = []
    for queue, beat in rows:
        try:
            if (now - datetime.fromisoformat(beat)).total_seconds() < _WORKER_GRACE_SECONDS:
                recent.append(queue)
        except (TypeError, ValueError):
            continue
    return recent


def _table_counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        names = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {name: int(conn.execute(f'SELECT COUNT(*) FROM "{name.replace(chr(34), chr(34) * 2)}"').fetchone()[0])
                for name in names}
    finally:
        conn.close()


def _copy_sqlite(source: Path, target: Path) -> dict[str, int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    os.chmod(target, stat.S_IMODE(source.stat().st_mode))
    conn = sqlite3.connect(target)
    try:
        conn.execute("PRAGMA journal_mode = DELETE")  # one self-contained file until the app reopens it
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if str(result).lower() != "ok":
        raise MigrationError(f"integrity_check failed for {target}: {result}")
    before, after = _table_counts(source), _table_counts(target)
    if before != after:
        raise MigrationError(f"row counts differ after copying {source.name}: {before} != {after}")
    return before


def _by_top_level(relative_paths: list[str]) -> dict[str, int]:
    groups: dict[str, int] = {}
    for rel in relative_paths:
        parts = PurePosixPath(rel).parts
        top = "/".join(parts[:2]) if parts[0] == "web" else parts[0]
        groups[top] = groups.get(top, 0) + 1
    return groups


def _write_json(target: Path, document) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _relocated_record(record: dict, plan: Plan) -> dict:
    record = dict(record)
    for name in _PATH_FIELDS:
        if record.get(name):
            record[name] = _relocate(str(record[name]), plan)
    return record


def _apply(plan: Plan) -> dict:
    counts: dict[str, dict[str, int]] = {}
    for step in plan.steps:
        if step.kind == "registry":
            document = json.loads(step.source.read_text(encoding="utf-8"))
            document["records"] = [_relocated_record(record, plan) for record in document.get("records", [])]
            migration = document.get("migration")
            if isinstance(migration, dict) and migration.get("backup_path"):
                migration["backup_path"] = _relocate(str(migration["backup_path"]), plan)
            _write_json(step.target, document)
        elif step.kind == "manifest":
            document = json.loads(step.source.read_text(encoding="utf-8"))
            _write_json(step.target, _relocated_record(document, plan) if isinstance(document, dict) else document)
        elif step.kind == "sqlite":
            counts[str(step.source)] = _copy_sqlite(step.source, step.target)
        else:
            step.target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(step.source, step.target)
            if _sha256(step.source) != _sha256(step.target):
                raise MigrationError(f"copy of {step.source} does not match")
    return counts


def _verify_with_app_code(plan: Plan) -> dict:
    """The checks the app itself makes, run on the new storage."""

    from dataset_catalog import activation, registry

    with paths.verifying_migration():
        registry_file = registry.registry_path()
        before = registry_file.read_bytes() if registry_file.is_file() else b""
        records = registry.load_registry()
        if registry_file.is_file() and registry_file.read_bytes() != before:
            raise MigrationError("the migrated registry needed repair when loaded; the path rewrite is wrong")
        expected = len(json.loads(before).get("records", [])) if before else 0
        if len(records) != expected:
            raise MigrationError(f"{expected - len(records)} dataset record(s) were quarantined after the migration")
        ready = 0
        for record in records:
            if record.status != "ready":
                continue
            try:
                activation._check_ready_kb(record, install_vocabulary=False)
            except activation.DatasetNotReady as exc:
                raise MigrationError(f"dataset {record.dataset_id} would not open: {exc.code}") from exc
            ready += 1
        missing_sources = [record.dataset_id for record in records if not Path(record.file_path).is_file()]
    return {"datasets": len(records), "ready_checked": ready, "missing_sources": missing_sources}


def _web_summary(db_path: Path) -> dict:
    if not db_path.is_file():
        return {}
    counts = _table_counts(db_path)
    conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    try:
        version = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    except sqlite3.Error:
        version = 0
    finally:
        conn.close()
    return {"schema_version": int(version), "accounts": counts.get("users", 0), "messages": counts.get("messages", 0)}


def migrate(home: Path, *, apply: bool, out=None) -> int:
    out = out or sys.stdout
    legacy = home / paths.LEGACY_STORE_NAME
    storage = paths.storage_root(home)
    if (storage / paths.MIGRATION_RECORD_NAME).is_file():
        print(f"already migrated (see {storage / paths.MIGRATION_RECORD_NAME})", file=out)
        return 0
    if not legacy.is_dir():
        print(f"nothing to migrate: no {legacy}", file=out)
        return 0
    leftovers = [p.name for p in storage.iterdir() if p.name not in {".gitignore", *_IGNORED_NAMES}] \
        if storage.is_dir() else []
    if leftovers:
        raise MigrationError(
            f"{storage} already holds {', '.join(sorted(leftovers))} from an unfinished migration; move those "
            "away (keep .gitignore), then run again. dataset_store/ was never changed."
        )
    recent = _recent_workers(legacy / "web" / "finx_web.sqlite3")
    if recent:
        raise MigrationError(f"the {', '.join(recent)} worker reported less than a minute ago; stop it first")
    busy = _held_locks(legacy)
    if busy:
        raise MigrationError(f"in use: {', '.join(busy)}; stop `agentfinx serve`, the workers and any build first")

    plan = build_plan(home)
    by_kind: dict[str, int] = {}
    for step in plan.steps:
        by_kind[step.kind] = by_kind.get(step.kind, 0) + 1
    total_bytes = sum(step.source.stat().st_size for step in plan.steps)
    print(f"from {legacy}\n  to {storage}", file=out)
    print(f"{len(plan.steps)} files ({total_bytes / 1e6:.1f} MB): "
          + ", ".join(f"{n} {kind}" for kind, n in sorted(by_kind.items())), file=out)
    for source, target in plan.reports.items():
        print(f"  report  {paths.to_stored(source)} -> {paths.to_stored(target)}", file=out)
    for top, n in _by_top_level(plan.unknown).items():
        print(f"  legacy  {top} ({n} files) -> storage/legacy/{top}", file=out)
    for top, n in _by_top_level(plan.skipped).items():
        print(f"  skip    {top} ({n} files, runtime only)", file=out)
    for warning in plan.warnings:
        print(f"  WARNING {warning}", file=out)
    if not apply:
        print("dry run: nothing changed. Run again with --apply to copy.", file=out)
        return 0

    with _hold_legacy_locks(legacy):
        counts = _apply(plan)
        result = _verify_with_app_code(plan)
        for source, before in counts.items():
            if _table_counts(Path(source)) != before:
                raise MigrationError(f"{source} changed while it was copied; stop `agentfinx serve` and retry")
        web = _web_summary(storage / "web" / "finx_web.sqlite3")
        record = {
            "migrated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "from": str(legacy),
            "files": len(plan.steps),
            "bytes": total_bytes,
            "sqlite_checked": len(counts),
            "reports": {paths.to_stored(s): paths.to_stored(t) for s, t in plan.reports.items()},
            "legacy": _by_top_level(plan.unknown),
            "skipped": _by_top_level(plan.skipped),
            "warnings": plan.warnings,
            "web": web,
            **result,
        }
        _write_json(storage / paths.MIGRATION_RECORD_NAME, record)
    print(f"migrated: {result['datasets']} datasets ({result['ready_checked']} ready, all open), "
          f"{web.get('accounts', 0)} accounts, {len(counts)} SQLite files checked.", file=out)
    print(f"{legacy} was not changed; delete it once the app works on the new storage.", file=out)
    return 0


def status(out=None) -> int:
    out = out or sys.stdout
    home = paths.home()
    storage = paths.storage_root(home)
    legacy = home / paths.LEGACY_STORE_NAME
    migrated = (storage / paths.MIGRATION_RECORD_NAME).is_file()
    print(f"FINX_HOME   {home}" + ("" if os.environ.get(paths.HOME_ENV) else "  (source checkout)"), file=out)
    print(f"storage     {storage}", file=out)
    print(f"artifacts   {home / 'artifacts'}", file=out)
    if legacy.is_dir():
        print(f"old layout  {legacy}  ({'migrated, can be deleted' if migrated else 'NOT migrated'})", file=out)
    try:
        paths.storage_dir()
    except paths.StorageError as exc:
        print(f"status      {exc}", file=out)
        return 1
    web_db = Path(os.environ.get(paths.WEB_DB_ENV, "") or paths.default_web_db_path())
    web = _web_summary(web_db)
    print(f"web db      {web_db}  " + (f"(schema v{web['schema_version']}, {web['accounts']} accounts)" if web
                                       else "(missing: create it with `agentfinx db init`)"), file=out)
    registry_file = paths.datasets_dir() / "registry.json"
    if registry_file.is_file():
        records = json.loads(registry_file.read_text(encoding="utf-8")).get("records", [])
        print(f"datasets    {len(records)}", file=out)
    else:
        print("datasets    0 (no registry yet)", file=out)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agentfinx storage", description="Where AgentFinX keeps its data.")
    commands = parser.add_subparsers(dest="command", required=True)
    migrate_parser = commands.add_parser("migrate", help="Copy dataset_store/ into the storage/ layout.")
    migrate_parser.add_argument("--apply", action="store_true", help="Copy and verify (default: dry run).")
    commands.add_parser("status", help="Show where the data is.")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            return status()
        return migrate(paths.home(), apply=args.apply)
    except (MigrationError, paths.StorageError) as exc:
        print(f"agentfinx storage: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
