from dataset_batch_result import build_prediction_latency_breakdown, build_summary


def test_prediction_breakdown_unions_parallel_model_stage_intervals():
    state = {
        "trace": [
            {
                "event": "planner:done",
                "timestamp": "2026-07-22T12:00:01+00:00",
                "duration_ms": 1000,
            },
            {
                "event": "router:done",
                "timestamp": "2026-07-22T12:00:01.500000+00:00",
                "duration_ms": 1000,
            },
            {
                "event": "evidence_tool:done",
                "timestamp": "2026-07-22T12:00:02+00:00",
                "duration_ms": 400,
            },
        ]
    }

    breakdown = build_prediction_latency_breakdown(state, prediction_e2e_ms=3000)

    assert breakdown["model_generation_ms"] == 1500
    assert breakdown["retrieval_local_ms"] == 1500
    assert breakdown["prediction_e2e_ms"] == 3000


def test_batch_summary_reports_latency_breakdown_separately():
    summary = build_summary(
        [
            {
                "errors": [],
                "latency_breakdown": {
                    "retrieval_local_ms": 400,
                    "model_generation_ms": 600,
                    "prediction_e2e_ms": 1000,
                },
            },
            {
                "errors": [],
                "latency_breakdown": {
                    "retrieval_local_ms": 600,
                    "model_generation_ms": 900,
                    "prediction_e2e_ms": 1500,
                },
            },
        ],
        [],
    )

    breakdown = summary["prediction_latency_breakdown"]
    assert breakdown["samples_n"] == 2
    assert breakdown["mean_retrieval_local_ms"] == 500
    assert breakdown["mean_model_generation_ms"] == 750
    assert breakdown["mean_prediction_e2e_ms"] == 1250
