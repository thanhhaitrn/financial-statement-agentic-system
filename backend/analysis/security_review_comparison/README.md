# Stored AI review comparison

Offline comparison requested on 2026-10-23. It preserves both prior reviews and
the main experiment. No human signoff, consensus label, or final scoring is
created. The eight comparison-only sensitivity labels are explicitly post-test.

From the repository root:

```sh
FINX_HOME="$PWD" PYTHONPATH=artifacts/eval/security/main-20261020/analysis-runtime-7d5d5817/backend/src .venv/bin/python -m backend.analysis.security_review_comparison.compare
```

The command validates the original packets, answer content, selected attempts,
report source hashes, and exact reproduction of both existing metric sets. It
writes only `artifacts/eval/security/main-20261020/review-comparison-20261023/`.
The report in that directory documents the interpretation and limitations.

`controlled_audit.py` reads an ongoing run using the current product runtime:

```sh
FINX_HOME="$PWD" PYTHONPATH=backend/src .venv/bin/python -m backend.analysis.security_review_comparison.controlled_audit
```

It does not open indexes or call the model. It captures complete log records,
verifies product/protocol/index identity and traces the attack article by fact
ID, including container-level decisions. Candidate acceptance is not counted as
actual calculation use. Re-running it replaces the local audit snapshot with a
later timestamp; the narrative report's timestamp/counts then need updating.

The only new code is analysis code; the scorer and running experiment are not
modified by this package.
