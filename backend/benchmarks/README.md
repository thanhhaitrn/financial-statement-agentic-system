# Benchmarks

Reviewed evaluation inputs, versioned with the code. Run outputs never go here:
they are written to `artifacts/eval/` (not tracked). Relative names in reports
and contracts are looked up here (`agentfinx.evaluation.benchmarks`).

| File | What it is |
|---|---|
| `dau_tu_APEC_ragas_seed.json` | 250 APEC questions with contexts and ground truth; the seed of `agentfinx predict` (profile `apec_q181_250`). |
| `queries_test.json` | 70 everyday report questions with references, for `agentfinx batch` and the routing/context evaluators. |
| `dau_tu_APEC.json` | 32 APEC instruction/output pairs with a difficulty label (older question set, kept for reference). |
| `reports/dau_tu_APEC.md` | The APEC report the factual-recall contract was written against. |
| `reports/document.md` | Sample report; the default dataset of `agentfinx ask`. |
| `ragas/manifest.json` | Artifact policy: what is versioned, the quality gate, latency rules. |
| `ragas/baseline_report_v2.csv` | Example of the report format, not a measured baseline. |

The factual-recall contract itself is a test fixture:
`backend/tests/fixtures/apec_q211_250_factual_facts.json`. The question sets the
evaluators use by default (`task_routing_contrastive.json`,
`web_intent_contrastive.json`, `context_contrastive.json`) ship with the package
in `src/agentfinx/evaluation/data/`.
