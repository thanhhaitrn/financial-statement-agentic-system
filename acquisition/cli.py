"""Synchronous, explicitly authorized report acquisition for a trusted local CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


def _parser():
    parser = argparse.ArgumentParser(prog="agentfinx reports")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("discover", "import", "status", "budget"):
        command = commands.add_parser(name)
        command.add_argument("--owner-id", default=f"local-{os.getuid()}",
                             help="Local account namespace; not an HTTP authentication mechanism")
        command.add_argument("--session-id", default="cli")
        if name in {"discover", "import"}:
            command.add_argument("--ticker", default="")
            command.add_argument("--company", default="")
            command.add_argument("--exchange", default="")
            command.add_argument("--year", dest="fiscal_year", type=int)
            command.add_argument("--quarter", dest="fiscal_quarter", type=int, choices=range(1, 5))
            command.add_argument("--scope", default="")
            command.add_argument("--audit-status", default="")
            command.add_argument("--report-type", default="financial_statement")
        if name == "import":
            source = command.add_mutually_exclusive_group(required=True)
            source.add_argument("--pdf", type=Path, help="Explicitly selected local PDF")
            source.add_argument("--confirmed-candidate-id", help="Selecting this ID confirms download")
            command.add_argument("--allow-cloud-parse", action="store_true",
                                 help="Consent to send this PDF to LlamaParse if local extraction fails")
            command.add_argument("--idempotency-key", default="")
        if name == "status":
            command.add_argument("job_id")
    return parser


def _default_service(*, allow_cloud_parse=False):
    from dotenv import load_dotenv
    load_dotenv()  # Never print environment values or credentials.
    from acquisition import build_default_acquisition_service
    from config.runtime_policy import RuntimePolicy
    return build_default_acquisition_service(
        RuntimePolicy.from_env(), allow_cloud_parse=allow_cloud_parse,
    )


def main(argv=None, *, service_factory=None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.owner_id.strip() or not args.session_id.strip():
        parser.error("owner-id and session-id must be non-empty")
    from schemas.acquisition import ReportQuery
    factory = service_factory or _default_service
    try:
        service = factory(allow_cloud_parse=getattr(args, "allow_cloud_parse", False))
        if args.command == "budget":
            result = service.quota.budget_state()
        elif args.command == "status":
            job = service.store.get_job(args.job_id, owner_id=args.owner_id)
            if job is None or job.session_id != args.session_id:
                raise ValueError("Import job not found in this account/session")
            result = job.model_dump(mode="json")
        else:
            metadata = {field: getattr(args, field) for field in (
                "ticker", "company", "exchange", "fiscal_year", "fiscal_quarter",
                "scope", "audit_status", "report_type",
            )}
            if args.command == "discover":
                if not args.ticker.strip():
                    parser.error("discover requires --ticker")
                candidates = service.discover(ReportQuery(
                    owner_id=args.owner_id, session_id=args.session_id, **metadata,
                ))
                result = {"candidates": [item.model_dump(mode="json") for item in candidates],
                          "next_step": "Confirm one candidate with reports import --confirmed-candidate-id ID"}
            else:
                # File validation precedes hashing and any reservation/network.
                if args.pdf:
                    artifact = service.validator.validate(args.pdf, origin="upload")
                    source_identity = artifact.source_sha256
                else:
                    source_identity = args.confirmed_candidate_id
                identity = json.dumps({"source": source_identity, "session": args.session_id,
                                       "metadata": metadata, "cloud": args.allow_cloud_parse,
                                       "converter": service.converter.converter_identity}, sort_keys=True)
                key = args.idempotency_key or hashlib.sha256(identity.encode()).hexdigest()
                common = dict(owner_id=args.owner_id, session_id=args.session_id, idempotency_key=key)
                if args.pdf:
                    job = service.create_upload_import(upload_path=args.pdf, metadata=metadata, **common)
                else:
                    job = service.create_candidate_import(candidate_id=args.confirmed_candidate_id, **common)
                job = service.run_job(job.job_id)
                result = job.model_dump(mode="json")
                if job.status == "ready":
                    result["next_step"] = f'agentfinx ask --dataset-id {job.dataset_id} --query "..."'
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if job.status == "ready" else 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        from acquisition.service import ReportDiscoveryUnavailable
        message = ("CafeF discovery unavailable; use reports import --pdf FILE instead."
                   if isinstance(exc, ReportDiscoveryUnavailable) else type(exc).__name__)
        print(json.dumps({"error": getattr(exc, "code", type(exc).__name__), "message": message},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
