"""``agentfinx eval-security``: ablation checks, the live pilot, rescoring.

    agentfinx eval-security validate              # no model call
    agentfinx eval-security pilot --allow-live    # real model, 300-call budget
    agentfinx eval-security score --out DIR       # rescore stored runs, no model call
    agentfinx eval-security preflight             # main run: every precondition, no model call
    agentfinx eval-security run --out DIR --allow-live   # main run: the protocol's 640 cells
    agentfinx eval-security score --out DIR --final      # main run: final report, review complete

``validate`` checks the inventory, the prompt switches (in fresh processes),
the case file, the fixture provider and the scorer samples. ``pilot`` runs
the required cases (I1, P2, I3) across A/B/C/D × clean/attack, then the
optional ones with ``--include-optional`` if budget remains, and scores
them. ``score`` applies the manual review files and rewrites the report.
``validate`` and ``score`` run under a zero-call budget, so a model request
would fail instead of being sent.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from agentfinx.core import experiment
from agentfinx.evaluation.security import cases as cases_module

_PROMPT_DUMP = """
import json
from agentfinx.core import ablation
from agentfinx.agents import prompts, synth_runner
from agentfinx.agents.profiles import AGENT_PROFILES
from agentfinx.news.analysis import prompts as news_prompts
texts = {
    "template": "\\n".join(section[1] for section in prompts._PROMPT_SECTIONS),
    "profiles": {name: profile["system_instruction"] for name, profile in AGENT_PROFILES.items()},
    "synth_news": synth_runner._WEB_NEWS_INSTRUCTION,
    "news_select": news_prompts.SELECT_AND_GROUP_SYSTEM,
    "news_evidence": news_prompts.SCORE_SYSTEM,
}
print(json.dumps({"config": ablation.config().as_dict(), "blocks": ablation.guard_blocks(), "texts": texts},
                 ensure_ascii=False))
"""


def dump_prompts(*, guards_off: bool, provenance_off: bool = False) -> dict:
    """The guarded prompt texts as a fresh process with these switches builds them."""

    from agentfinx.evaluation.security.runner import worker_env

    env = worker_env("D")
    env["AGENTFINX_ABLATE_PROMPT_GUARDS"] = "1" if guards_off else "0"
    env["AGENTFINX_ABLATE_PROVENANCE"] = "1" if provenance_off else "0"
    output = subprocess.run([sys.executable, "-c", _PROMPT_DUMP], env=env, capture_output=True, text=True,
                            timeout=300, check=True)
    return json.loads(output.stdout.strip().splitlines()[-1])


def _flatten(texts: dict) -> str:
    return json.dumps(texts, ensure_ascii=False)


def strip_guard_blocks(value, blocks: dict):
    """``value`` (text, or a dict of texts) with every named block replaced by
    its removal text: what the prompts must be with the guards off."""

    if isinstance(value, dict):
        return {key: strip_guard_blocks(item, blocks) for key, item in value.items()}
    for block in sorted((b for items in blocks.values() for b in items), key=lambda b: -len(b["text"])):
        value = value.replace(block["text"], block["removed"])
    return value


def prompt_switch_problems(on: dict, off: dict) -> list[str]:
    problems = []
    if on["config"]["prompt_guards_off"] or not off["config"]["prompt_guards_off"]:
        problems.append("prompt switch not read as requested")
    flat_on = _flatten(on["texts"])
    flat_off = _flatten(off["texts"])
    for defense_id, blocks in on["blocks"].items():
        for block in blocks:
            text = json.dumps(block["text"], ensure_ascii=False)[1:-1]
            if text not in flat_on:
                problems.append(f"{defense_id}: block missing with guards on")
            if text in flat_off:
                problems.append(f"{defense_id}: block still present with guards off")
    for name in on["texts"]:
        if strip_guard_blocks(on["texts"][name], on["blocks"]) != off["texts"][name]:
            problems.append(f"{name}: guards off changed more than the named blocks")
    for marker in ("<worker_results>", "<evidence_pack>", "<<<DU_LIEU>>>", "<<<HET_DU_LIEU>>>",
                   "Cite WEB fact URLs only in the relevant detailed section.", "[publisher](source_url)"):
        if marker not in flat_off:
            problems.append(f"business text or delimiter lost with guards off: {marker}")
    return problems


def validate(cases_path: str | None) -> int:
    from agentfinx.core import ablation
    from agentfinx.evaluation.security import checks, inventory, samples

    cases = cases_module.load_cases(cases_path)
    report: dict = {"cases_file": str(cases.path), "fixture_sha256": cases.sha256}
    with tempfile.TemporaryDirectory() as scratch:
        budget = Path(scratch) / "budget.json"
        experiment.create_budget(budget, 0)
        context = experiment.ExperimentContext(run_id="validate", budget_path=str(budget))
        with experiment.activate(context):
            on = dump_prompts(guards_off=False)
            off = dump_prompts(guards_off=True)
            report["prompt_switch"] = prompt_switch_problems(on, off)
            report["inventory_missing"] = inventory.missing_inventory_rows(on["blocks"])
            report["current_process_flags"] = ablation.config().as_dict()
            report["cases"] = {case_id: problems for case_id in cases.all_case_ids()
                               if (problems := checks.check_case(cases, case_id))}
            report["dataset"] = checks.check_datasets(cases)
            if cases.schema >= 2:
                report["attack_set"] = checks.check_attack_set(cases)
                report["counts"] = {split: len(cases.case_ids(split=split)) for split in ("dev", "test")}
            # The scorer samples are written on the pilot's cases (dev material).
            pilot_cases = cases_module.load_cases()
            sample_results = [samples.check(pilot_cases, sample) for sample in samples.SAMPLES]
            report["scorer_samples"] = {"passed": sum(ok for ok, _ in sample_results),
                                        "total": len(sample_results),
                                        "failed": [details for ok, details in sample_results if not ok]}
        report["model_calls"] = experiment.read_budget(budget)["used"] + experiment.read_budget(budget)["blocked"]
    failed = bool(report["prompt_switch"] or report["inventory_missing"] or report["cases"] or report["dataset"]
                  or report.get("attack_set")
                  or report["scorer_samples"]["failed"] or report["model_calls"]
                  or any(report["current_process_flags"].values()))
    report["ok"] = not failed
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 1 if failed else 0


def build(args) -> int:
    """Write the attack set from the spec and the dataset reports."""

    from agentfinx.evaluation import benchmarks
    from agentfinx.evaluation.security import builder

    path = Path(args.out) if args.out else benchmarks.resolve(builder.OUTPUT)
    if (path.parent / "FREEZE.json").exists():
        print(f"{path} is frozen; use a new directory for a new version", file=sys.stderr)
        return 2
    output = builder.build(args.spec)
    digest = builder.write(output, path)
    print(json.dumps({"path": str(path), "sha256": digest, "cases": len(output["cases"])}))
    return 0


def review_packet(args) -> int:
    from agentfinx.evaluation.security import freeze as freeze_module

    cases = cases_module.load_cases(args.cases or cases_module.ATTACK_SET)
    packet, signoff = freeze_module.write_review_packet(cases, args.n)
    print(json.dumps({"packet": str(packet), "signoff": str(signoff),
                      "cases": freeze_module.review_sample(cases, args.n)}, ensure_ascii=False))
    return 0


def freeze(args) -> int:
    from agentfinx.evaluation.security import freeze as freeze_module

    cases = cases_module.load_cases(args.cases or cases_module.ATTACK_SET)
    record = freeze_module.freeze(cases)
    print(json.dumps(record, ensure_ascii=False, indent=1))
    print("Commit FREEZE.json, attack_set.json and the sign-off now; record the hashes in the experiment notes.")
    return 0


def dev_review(args) -> int:
    """Record the completed two-reviewer review of dev run directories."""

    from agentfinx.evaluation.security import freeze as freeze_module

    cases = cases_module.load_cases(args.cases or cases_module.ATTACK_SET)
    record = freeze_module.record_dev_review(cases, [Path(item) for item in args.dir])
    print(json.dumps(record, ensure_ascii=False, indent=1))
    return 0


def exclude(args) -> int:
    from agentfinx.evaluation.security import freeze as freeze_module

    cases = cases_module.load_cases(args.cases or cases_module.ATTACK_SET)
    print(json.dumps(freeze_module.exclude(cases, args.case_id, args.reason), ensure_ascii=False))
    return 0


def default_out() -> Path:
    from agentfinx.core.config import paths

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return paths.eval_runs_dir() / "security" / f"pilot-{stamp}"


def pilot(args) -> int:
    from agentfinx.evaluation.security.runner import DEFAULT_BUDGET, Pilot

    if not args.allow_live:
        print("pilot calls the real model; pass --allow-live to run it", file=sys.stderr)
        return 2
    if args.budget > DEFAULT_BUDGET:
        print(f"the budget is at most {DEFAULT_BUDGET} model calls", file=sys.stderr)
        return 2
    cases = cases_module.load_cases(args.cases)
    out = Path(args.out) if args.out else default_out()
    if cases.schema >= 2:
        from agentfinx.evaluation.security import freeze as freeze_module

        ids = args.case_ids.split(",") if args.case_ids else cases.case_ids(split=args.split)
        if any(cases.case(case_id)["split"] == "test" for case_id in ids):
            problems = freeze_module.frozen_problems(cases)
            if problems:
                print("the test split runs only from a frozen attack set: " + "; ".join(problems), file=sys.stderr)
                return 2
        dropped = freeze_module.excluded(cases)
        ids = [case_id for case_id in ids if case_id not in dropped]
    else:
        ids = cases.case_ids(include_optional=args.include_optional)
    keys = _keys(args)
    if keys is None:
        return 2
    runner = Pilot(out, cases, case_ids=ids, budget=args.budget, keys=keys if keys else None,
                   retry_exhausted_keys=args.retry_exhausted_keys, configs=args.configs.split(","),
                   conditions=args.conditions.split(","))
    def log(message: str) -> None:
        print(message, flush=True)  # progress is followed live from a log file

    log(f"experiment directory: {out}")
    log(f"Ollama credential failover: {len(keys)} unique configured keys (values never logged)")
    runner.prepare()
    status = runner.run(log=log)
    log(json.dumps(status, indent=1))
    _score(out, cases)
    log(f"report: {out / 'pilot_report.md'}")
    return 3 if status.get("stopped") == "all_keys_exhausted" else 0


def _keys(args) -> list[str] | None:
    """Configured Ollama keys for failover (the active one first); None on a bad path."""

    import os

    from agentfinx.core.config import paths
    from agentfinx.core.llm.keys import load_keys

    keys_file = Path(args.keys_file) if args.keys_file else paths.home() / ".env"
    if args.keys_file and not keys_file.is_file():
        print("Configured keys file does not exist", file=sys.stderr)
        return None
    keys = load_keys(keys_file, include_commented=args.include_commented_keys) if keys_file.is_file() else []
    active_key = os.getenv("OLLAMA_API_KEY", "").strip()
    return list(dict.fromkeys(([active_key] if active_key else []) + keys))


def controlled(args) -> int:
    """The controlled-exposure check: attack articles straight to the workers'
    fact input as WEB facts (no news analysis), within what the natural pilot
    left of the 300-call budget. Labelled apart from the natural pilot."""

    from agentfinx.evaluation.security.runner import DEFAULT_BUDGET, Pilot

    if not args.allow_live:
        print("this check calls the real model; pass --allow-live to run it", file=sys.stderr)
        return 2
    parent = Path(args.parent)
    parent_manifest = json.loads((parent / "manifest.json").read_text(encoding="utf-8"))
    # One global budget: the pilot and every controlled check made for it.
    used = int(experiment.read_budget(parent / "budget.json").get("used", 0))
    for manifest_path in parent.parent.glob("*/manifest.json"):
        other = json.loads(manifest_path.read_text(encoding="utf-8"))
        if other.get("exposure") == "controlled" and Path(other.get("parent", "")).resolve() == parent.resolve():
            used += int(experiment.read_budget(manifest_path.parent / "budget.json").get("used", 0))
    remaining = DEFAULT_BUDGET - used
    if remaining <= 0:
        print("no budget left after the natural pilot", file=sys.stderr)
        return 2
    cases = cases_module.load_cases(parent_manifest["cases_file"])
    if cases.sha256 != parent_manifest["fingerprint"]["payload"]["fixture_sha256"]:
        print("the case file changed since the pilot", file=sys.stderr)
        return 2
    keys = _keys(args)
    if keys is None:
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) if args.out else parent.parent / f"{parent.name}-controlled-{stamp}"
    runner = Pilot(out, cases, case_ids=args.case_ids.split(","), budget=remaining,
                   keys=keys if keys else None, exposure="controlled", configs=args.configs.split(","),
                   conditions=("attack",), parent=str(parent))

    def log(message: str) -> None:
        print(message, flush=True)

    log(f"controlled-exposure directory: {out} (budget {remaining} = {DEFAULT_BUDGET} - {used} already used)")
    runner.prepare()
    status = runner.run(log=log)
    log(json.dumps(status, indent=1))
    _score(out, cases)
    _score(parent, cases)  # the pilot report links the check
    log(f"report: {out / 'pilot_report.md'}")
    return 0


def _score(out: Path, cases) -> dict:
    from agentfinx.evaluation.security.report import score_experiment

    with tempfile.TemporaryDirectory() as scratch:
        budget = Path(scratch) / "budget.json"
        experiment.create_budget(budget, 0)
        with experiment.activate(experiment.ExperimentContext(run_id="score", budget_path=str(budget))):
            return score_experiment(out, cases)


def _offline(fn, *args, **kwargs):
    """Run ``fn`` under a zero-call budget: a model request fails instead of being sent."""

    with tempfile.TemporaryDirectory() as scratch:
        budget = Path(scratch) / "budget.json"
        experiment.create_budget(budget, 0)
        with experiment.activate(experiment.ExperimentContext(run_id="offline", budget_path=str(budget))):
            value = fn(*args, **kwargs)
        calls = experiment.read_budget(budget)
    if calls["used"] or calls["blocked"]:
        raise SystemExit("an offline step asked for a model call")
    return value


def preflight(args) -> int:
    """Every precondition of the main run, without a model call."""

    from agentfinx.evaluation.security.main_run import preflight as check_main

    report = _offline(check_main, args.protocol)
    text = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if args.out:
        Path(args.out).mkdir(parents=True, exist_ok=True)
        (Path(args.out) / "preflight.json").write_text(text, encoding="utf-8")
    print(text)
    return 0 if report["ok"] else 1


def run_main(args) -> int:
    """The 640 main runs of the protocol, with its own budget ledger."""

    from agentfinx.evaluation.security.main_run import MainRun

    if not args.allow_live:
        print("run calls the real model; pass --allow-live to run it", file=sys.stderr)
        return 2
    keys = _keys(args)
    if keys is None:
        return 2
    out = Path(args.out)
    runner = MainRun(out, args.protocol, keys=keys if keys else None,
                     retry_exhausted_keys=args.retry_exhausted_keys)

    def log(message: str) -> None:
        print(message, flush=True)

    log(f"main experiment directory: {out}")
    log(f"Ollama credential failover: {len(keys)} unique configured keys (values never logged)")
    status = runner.run(log=log)
    log(json.dumps({key: value for key, value in status.items() if key != "missing"}, indent=1))
    from agentfinx.evaluation.security.main_report import score_main

    _offline(score_main, out)
    log(f"provisional report: {out / 'main_report.md'}")
    return 3 if status.get("stopped") == "all_keys_exhausted" else 0


def score(args) -> int:
    out = Path(args.out)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("protocol_sha256"):
        from agentfinx.evaluation.security.main_report import score_main

        summary = _offline(score_main, out, final=args.final)
        name = "main_report_final.md" if args.final else "main_report.md"
        print(f"{summary['cells']['valid']}/{summary['cells']['expected']} cells valid; report: {out / name}")
        return 0
    if args.final:
        print("--final applies to the main experiment only", file=sys.stderr)
        return 2
    cases = cases_module.load_cases(args.cases or manifest["cases_file"])
    if cases.sha256 != manifest["fingerprint"]["payload"]["fixture_sha256"]:
        print("the case file changed since the pilot; refusing to rescore", file=sys.stderr)
        return 2
    summary = _score(out, cases)
    valid = sum(1 for run in summary["runs"] if run["valid"])
    print(f"{valid}/{len(summary['runs'])} valid runs scored; report: {out / 'pilot_report.md'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentfinx eval-security", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_validate = sub.add_parser("validate", help="check switches, cases, provider and scorer (no model call)")
    p_validate.add_argument("--cases", default=None)
    p_build = sub.add_parser("build", help="write the attack set from the spec and the reports (no model call)")
    p_build.add_argument("--spec", default=None)
    p_build.add_argument("--out", default=None)
    p_build.add_argument("--force", action="store_true", help="legacy option; never overwrites a frozen set")
    p_pilot = sub.add_parser("pilot", help="run the live pilot")
    p_pilot.add_argument("--allow-live", action="store_true")
    p_pilot.add_argument("--out", default=None)
    p_pilot.add_argument("--cases", default=None)
    p_pilot.add_argument("--budget", type=int, default=300)
    p_pilot.add_argument("--include-optional", action="store_true", help="also P3 and P4 if budget remains")
    p_pilot.add_argument("--split", choices=["dev", "test"], default="dev", help="attack set split (schema 2)")
    p_pilot.add_argument("--case-ids", default="", help="comma-separated case ids (schema 2)")
    p_pilot.add_argument("--configs", default="A,B,C,D")
    p_pilot.add_argument("--conditions", default="clean,attack")
    p_pilot.add_argument("--keys-file", help="key list; defaults to FINX_HOME/.env; active environment key is tried first")
    p_pilot.add_argument("--include-commented-keys", action="store_true",
                         help="explicitly include commented OLLAMA_API_KEY entries as backups")
    p_pilot.add_argument("--retry-exhausted-keys", action="store_true",
                         help="retry previously exhausted credentials after provider quota resets; keeps call budget")
    p_ctrl = sub.add_parser("controlled", help="controlled-exposure check at the workers' fact input")
    p_ctrl.add_argument("--allow-live", action="store_true")
    p_ctrl.add_argument("--parent", required=True, help="the natural pilot directory")
    p_ctrl.add_argument("--out", default=None)
    p_ctrl.add_argument("--case-ids", default="I1,I3")
    p_ctrl.add_argument("--configs", default="B,C,D")
    p_ctrl.add_argument("--keys-file")
    p_ctrl.add_argument("--include-commented-keys", action="store_true")
    p_packet = sub.add_parser("review-packet", help="sample test cases for a second person to check")
    p_packet.add_argument("--cases", default=None)
    p_packet.add_argument("--n", type=int, default=15)
    p_freeze = sub.add_parser("freeze", help="record the hashes of the reviewed attack set (FREEZE.json)")
    p_freeze.add_argument("--cases", default=None)
    p_dev = sub.add_parser("dev-review", help="record dev review, or technical validation under an explicit protocol waiver")
    p_dev.add_argument("--cases", default=None)
    p_dev.add_argument("--dir", action="append", required=True, help="a dev run directory (repeatable)")
    p_exclude = sub.add_parser("exclude", help="mark a frozen case broken, with its reason (append-only)")
    p_exclude.add_argument("--cases", default=None)
    p_exclude.add_argument("case_id")
    p_exclude.add_argument("--reason", required=True)
    p_score = sub.add_parser("score", help="rescore stored runs and apply manual review (no model call)")
    p_score.add_argument("--out", required=True)
    p_score.add_argument("--cases", default=None)
    p_score.add_argument("--final", action="store_true",
                         help="main experiment: the final report; refuses while required review is missing")
    p_pre = sub.add_parser("preflight", help="check every precondition of the main run (no model call)")
    p_pre.add_argument("--protocol", default=None)
    p_pre.add_argument("--out", default=None, help="also write preflight.json here")
    p_run = sub.add_parser("run", help="the main experiment of a frozen protocol (its own budget)")
    p_run.add_argument("--protocol", default=None)
    p_run.add_argument("--out", required=True)
    p_run.add_argument("--allow-live", action="store_true")
    p_run.add_argument("--keys-file")
    p_run.add_argument("--include-commented-keys", action="store_true")
    p_run.add_argument("--retry-exhausted-keys", action="store_true",
                       help="retry exhausted credentials after the provider quota resets; keeps the budget")
    args = parser.parse_args(argv)
    if args.command == "validate":
        return validate(args.cases)
    if args.command == "build":
        return build(args)
    if args.command == "pilot":
        return pilot(args)
    if args.command == "controlled":
        return controlled(args)
    if args.command == "review-packet":
        return review_packet(args)
    if args.command == "freeze":
        return freeze(args)
    if args.command == "exclude":
        return exclude(args)
    if args.command == "dev-review":
        return dev_review(args)
    if args.command == "preflight":
        return preflight(args)
    if args.command == "run":
        return run_main(args)
    return score(args)


if __name__ == "__main__":
    sys.exit(main())
