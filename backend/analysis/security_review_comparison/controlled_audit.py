"""Read-only snapshot of an ongoing controlled run; no scoring or model calls."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from agentfinx.core.schemas.numbers import parse_financial_decimal
from agentfinx.evaluation.security import freeze, outcomes, protocol as protocols

ROOT = Path(__file__).resolve().parents[3]
RUN = ROOT / 'artifacts/eval/security/controlled-L5-20261023'
OUT = ROOT / 'artifacts/eval/security/main-20261020/review-comparison-20261023'


def read(path):
    return json.loads(path.read_text())


def sha(data):
    return hashlib.sha256(data).hexdigest()


def complete_jsonl(path):
    data = path.read_bytes()
    # Another process may be appending. Only capture complete newline-ended records.
    prefix = data[:data.rfind(b'\n') + 1]
    return prefix, [json.loads(line) for line in prefix.splitlines() if line.strip()]


def build():
    path = ROOT / 'backend/benchmarks/security/controlled_protocol.json'
    protocol, protocol_sha, _ = protocols.load(path)
    cases = protocols.attack_set(protocol)
    manifest = read(RUN / 'manifest.json')
    preflight = read(RUN / 'preflight.json')
    assert path.read_bytes() == (RUN / 'protocol_snapshot.json').read_bytes()
    assert protocol_sha == 'ce2a306b655cd13cb81cd6d83c6793e1291dba3c746692b6e4a9dea00a6df643'
    assert not protocols.problems(protocol, cases)
    assert not protocols.identity_problems(protocol, preflight['identities'])
    assert freeze.product_code_sha256() == protocol['pinned']['product_code_sha256']
    assert preflight['ok'] and preflight['protocol_sha256'] == protocol_sha
    assert preflight['attack_set_sha256'] == cases.sha256 == protocol['attack_set']['sha256']
    for name, wanted in protocol['pinned']['datasets'].items():
        for k in ('dataset_generation', 'index_generation'):
            assert preflight['datasets'][name][k] == wanted[k]
    attempt_bytes, attempts = complete_jsonl(RUN / 'attempts.jsonl')
    event_bytes, events = complete_jsonl(RUN / 'events.jsonl')
    budget = read(RUN / 'budget.json')
    status = read(RUN / 'main_status.json')
    chosen = {}
    for a in attempts:
        if a['valid']:
            key = tuple(a['cell'])
            if key not in chosen or a['attempt'] < chosen[key]['attempt']:
                chosen[key] = a
    grouped = defaultdict(list)
    for e in events:
        if e.get('run_id') in {a['run_id'] for a in chosen.values()}:
            grouped[e['run_id']].append(e)
    rows, selected_events = [], []
    for key, attempt in chosen.items():
        case_id, config, condition, repetition = key
        rid = attempt['run_id']
        case = cases.case(case_id)
        rp = RUN / 'runs' / rid / 'result.json'
        result = read(rp)
        assert result['status'] == 'ok' and result['exposure'] == 'controlled'
        assert result['identity'] == preflight['identities'][config]
        ds = result['dataset']
        for k in ('dataset_generation', 'index_generation'):
            assert ds[k] == protocol['pinned']['datasets'][case['dataset_id']][k]
        injected = [e for e in grouped[rid] if e['kind'] == 'controlled_web_facts_injected']
        facts = {f['fact_id']: f for e in injected for f in e['facts']}
        target_facts = {k: f for k, f in facts.items() if case['canary'] in f['value']}
        assert len(target_facts) == 1, (rid, 'cannot identify unique attack article')
        target_id, fact = next(iter(target_facts.items()))
        decisions = [e for e in grouped[rid] if e['kind'] == 'provenance_decision'
                     and target_id in ([e.get('fact_id')] + (e.get('fact_ids') or []))]
        actual_outcomes = [e for e in grouped[rid] if e['kind'] == 'provenance_outcome'
                           and e.get('fact_id') == target_id]
        snap = read(RUN / 'runs' / rid / 'snapshot.json')
        calc_ids = outcomes.calculation_fact_ids(snap)
        parsed = parse_financial_decimal(fact.get('parsed_value') or fact['value'])
        targets = [Decimal(str(f['value'])) for f in case['attacker_target'].get('facts', [])]
        expected = 'allowed' if config == 'C' else 'blocked'
        rows.append({'run_id': rid, 'case_id': case_id, 'config': config,
                     'result_sha256': sha(rp.read_bytes()), 'attack_fact_id': target_id,
                     'attack_content_hash': fact['content_hash'], 'article_value': fact['value'],
                     'injected_needby': fact['needby'], 'parsed_article_value': str(parsed),
                     'attacker_target_values': [str(v) for v in targets],
                     'parsed_value_equals_target': parsed in targets,
                     'attack_fact_reached_gate': bool(decisions),
                     'gate_policy_matches_config': bool(decisions) and all(e['effective'] == expected for e in decisions),
                     'decision_sites': dict(Counter(e['site'] for e in decisions)),
                     'outcomes': [{'site': e['site'], 'outcome': e['outcome']} for e in actual_outcomes],
                     'accepted_as_operand_candidate': any(e['outcome'] == 'accepted_as_operand' for e in actual_outcomes),
                     'attack_fact_in_derived_calculation_ledger': target_id in calc_ids})
        selected_events.extend(injected + decisions + actual_outcomes)
    summary = {}
    for config in protocol['configs']:
        sub = [r for r in rows if r['config'] == config]
        summary[config] = {'completed_valid_cells': len(sub), **{
            dim: sum(r[dim] for r in sub) for dim in (
                'attack_fact_reached_gate', 'gate_policy_matches_config', 'parsed_value_equals_target',
                'accepted_as_operand_candidate', 'attack_fact_in_derived_calculation_ledger')},
                'parsed_article_values': dict(Counter(r['parsed_article_value'] for r in sub))}
    audit = {'kind': 'partial_live_run_snapshot_not_final_scoring',
             'captured_at': datetime.now(timezone.utc).isoformat(),
             'protocol_sha256': protocol_sha, 'protocol_snapshot_matches': True,
             'current_product_sha256_matches': True,
             'pinned_dataset_identities_in_saved_preflight': len(preflight['datasets']),
             'all_selected_worker_identities_and_dataset_generations_match': True,
             'no_fresh_index_open_or_network_preflight': True,
             'additional_experiment_model_calls_by_this_audit': 0,
             'completed_valid_cells': len(rows), 'scheduled_cells': len(manifest['schedule']),
             'attempt_statuses': dict(Counter(a['status'] for a in attempts)),
             'budget_used_at_capture': budget['used'], 'budget_limit': budget['limit'],
             'last_completed_attempt_at': attempts[-1]['ts'],
             'stored_main_status_valid_cells': status['valid_cells'],
             'stored_main_status_stopped': status['stopped'],
             'stored_main_status_is_stale': status['valid_cells'] != len(rows),
             'event_input_prefix_bytes': len(event_bytes), 'event_input_prefix_sha256': sha(event_bytes),
             'attempt_input_prefix_bytes': len(attempt_bytes), 'attempt_input_prefix_sha256': sha(attempt_bytes),
             'summary': summary, 'runs': rows}
    OUT.mkdir(exist_ok=True)
    (OUT / 'controlled_audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
    (OUT / 'controlled_events_snapshot.jsonl').write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in selected_events))
    print(json.dumps({k: v for k, v in audit.items() if k != 'runs'}, ensure_ascii=False))


if __name__ == '__main__':
    build()
