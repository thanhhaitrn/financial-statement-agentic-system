"""``agentfinx datasets gc``: remove superseded Qdrant builds of one dataset.

Every build goes into its own physical collection
(``<logical>__v_<fingerprint>_<suffix>``); once validated, the
``<logical>__active`` alias is switched to it and older builds stay behind.
GC keeps the collection the alias points at, the newest previous build, and
anything younger than 48 hours. A collection without a creation timestamp
(built before timestamps were recorded) is never deleted — its age is unknown.
Without an active alias nothing is deleted.

Dry-run by default; ``--apply`` deletes while holding the dataset lock, so no
build, publish or query of that dataset runs at the same time.
"""
# Code note: Dataset modules manage local registry records and derived artifact paths.

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

MIN_AGE = timedelta(hours=48)


@dataclass(frozen=True)
class CollectionDecision:
    name: str
    created_at: str
    action: str  # "keep" | "delete"
    reason: str


def _parse_time(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def plan_collection_gc(
    logical_name: str,
    *,
    client: Any,
    now: datetime,
    min_age: timedelta = MIN_AGE,
) -> list[CollectionDecision]:
    from agentfinx.datasets.vectorstore.qdrant_store import _active_alias_name, _alias_targets

    prefix = f"{logical_name}__v_"
    active = _alias_targets(client).get(_active_alias_name(logical_name))
    builds: list[tuple[str, str, Optional[datetime]]] = []
    for collection in client.get_collections().collections:
        name = str(collection.name)
        if not name.startswith(prefix):
            continue
        info = client.get_collection(collection_name=name)
        metadata = getattr(info.config, "metadata", None) or {}
        raw = str(metadata.get("created_at") or "")
        builds.append((name, raw, _parse_time(raw) if raw else None))

    # "Previous" is the newest build older than the active one. A build newer
    # than the active one was staged but never activated; it only gets the
    # 48-hour grace period.
    active_created = next((created for name, _raw, created in builds if name == active), None)
    previous_builds = sorted(
        (
            build
            for build in builds
            if build[0] != active
            and build[2] is not None
            and (active_created is None or build[2] < active_created)
        ),
        key=lambda build: build[2],
        reverse=True,
    )
    previous = previous_builds[0][0] if previous_builds else None

    decisions = []
    for name, raw, created in sorted(builds):
        if active is None:
            action, reason = "keep", "no_active_alias"
        elif name == active:
            action, reason = "keep", "active"
        elif created is None:
            action, reason = "keep", "no_timestamp"
        elif name == previous:
            action, reason = "keep", "previous"
        elif now - created < min_age:
            action, reason = "keep", "younger_than_48h"
        else:
            action, reason = "delete", "superseded"
        decisions.append(CollectionDecision(name, raw, action, reason))
    return decisions


def collect_dataset(
    dataset_id: str,
    *,
    apply: bool = False,
    client: Any = None,
    now: Optional[datetime] = None,
) -> dict:
    from agentfinx.datasets.locks import dataset_lock
    from agentfinx.datasets.registry import get_dataset

    record = get_dataset(dataset_id)
    if record is None:
        raise LookupError(f"dataset not found: {dataset_id}")
    if client is None:
        from agentfinx.datasets.vectorstore.qdrant_store import get_client

        client = get_client()
    current = now or datetime.now(timezone.utc)
    deleted = []
    with dataset_lock(record.dataset_id):
        decisions = plan_collection_gc(record.vector_collection_name, client=client, now=current)
        if apply:
            for decision in decisions:
                if decision.action == "delete":
                    client.delete_collection(collection_name=decision.name)
                    deleted.append(decision.name)
    return {
        "dataset_id": record.dataset_id,
        "applied": apply,
        "deleted": deleted,
        "collections": [asdict(decision) for decision in decisions],
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agentfinx datasets")
    commands = parser.add_subparsers(dest="command", required=True)
    gc = commands.add_parser(
        "gc", help="Remove superseded Qdrant builds of one dataset (dry-run by default)"
    )
    gc.add_argument("--dataset-id", required=True)
    gc.add_argument("--apply", action="store_true", help="Delete; without it only the plan is printed")
    args = parser.parse_args(argv)

    from agentfinx.core.config import paths

    paths.load_env()  # Qdrant settings; never printed.
    try:
        result = collect_dataset(args.dataset_id, apply=args.apply)
    except (LookupError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
