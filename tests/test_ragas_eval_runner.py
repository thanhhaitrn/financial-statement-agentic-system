import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from dataset_batch_result import (
    METRIC_NAMES,
    extract_retrieved_contexts,
    is_session_limit_error,
    load_seed_records,
    merge_predictions_for_records,
    predictions_by_key,
)
from ragas_eval_runner import (
    _is_event_loop_closed_error,
    _latency_contamination_messages,
    _metric_failures,
    _source_prediction_status,
    apply_evaluation_contract,
    atomic_write_report_pair,
    build_latency_contract,
    build_ragas_evaluation_samples,
    merge_scores_for_predictions,
)
from eval_retrieval_recall import (
    FACTUAL_RECALL_THRESHOLD,
    attach_expected_facts_from_seed,
    attach_expected_facts_from_contract,
    evaluate_factual_recall,
    fact_matches,
    facts_from_contexts,
    normalize_fact,
)
from analyze_batch_metrics import (
    analyze,
    parse_args as parse_analysis_args,
    summarize_clean_latency_baseline,
    summarize_repeated_judge_variance,
)


class RagasEvalRunnerTests(unittest.TestCase):
    @staticmethod
    def _reviewed_prediction(*, with_context: bool = True) -> dict:
        payload = json.loads(
            (ROOT_DIR / "tests" / "fixtures" / "apec_q211_250_factual_facts.json")
            .read_text(encoding="utf-8")
        )
        record = payload["records"][0]
        fact = record["expected_facts"][0]
        context = "\n".join(
            (
                f"Entity: {fact['entity']}",
                f"Metric: {fact['metric']}",
                f"Period: {fact['period']}",
                f"Value: {fact['value']}",
                f"Unit: {fact['unit']}",
                f"Reference: {fact['reference']}",
            )
        )
        return {
            "id": record["id"],
            "question": record["question"],
            "retrieved_contexts": [context] if with_context else [],
            "runtime": 100,
            "tokens": 10,
            "errors": [],
        }

    def test_load_seed_records_apec(self):
        records = load_seed_records(ROOT_DIR / "dau_tu_APEC_ragas_seed.json")

        self.assertEqual(len(records), 250)
        self.assertTrue(all(record["question"] for record in records))
        self.assertTrue(all(record["ground_truth"] for record in records))
        self.assertTrue(all(record["seed_contexts"] for record in records))

    def test_extract_retrieved_contexts_from_evidence_pack(self):
        final_state = {
            "evidence_pack": {
                "facts_by_table": {
                    "BANG CAN DOI KE TOAN": {
                        "facts": [
                            {
                                "company": "Công ty APEC",
                                "table": "BANG CAN DOI KE TOAN",
                                "item_name": "Tong tai san",
                                "value": "100",
                                "reference": "V.1",
                                "source": "Bang can doi ke toan",
                            },
                            {
                                "company": "Công ty APEC",
                                "table": "BANG CAN DOI KE TOAN",
                                "item_name": "Tong tai san",
                                "value": "100",
                                "reference": "V.1",
                                "source": "Bang can doi ke toan",
                            },
                        ]
                    }
                }
            }
        }

        contexts = extract_retrieved_contexts(final_state)

        self.assertEqual(len(contexts), 1)
        self.assertIn("Table: BANG CAN DOI KE TOAN", contexts[0])
        self.assertIn("Entity: Công ty APEC", contexts[0])
        self.assertIn("Item: Tong tai san", contexts[0])
        self.assertIn("Value: 100", contexts[0])
        self.assertIn("Reference: V.1", contexts[0])
        self.assertNotIn("Hint:", contexts[0])

    def test_extract_retrieved_contexts_prefers_compact_ragas_facts_and_omits_hint(self):
        final_state = {
            "ragas_facts_by_table": {
                "THUYET MINH": {
                    "facts": [
                        {
                            "table": "THUYET MINH",
                            "item_name": "Tien",
                            "value": "100",
                            "evidence_text": "Full retrieved document without truncation.",
                        }
                    ]
                }
            },
            "evidence_pack": {
                "facts_by_table": {
                    "THUYET MINH": {
                        "facts": [
                            {
                                "table": "THUYET MINH",
                                "item_name": "Tien",
                                "value": "100",
                                "message": "Compact message",
                            }
                        ]
                    }
                }
            },
        }

        contexts = extract_retrieved_contexts(final_state)

        self.assertEqual(len(contexts), 1)
        self.assertIn("Table: THUYET MINH", contexts[0])
        self.assertIn("Item: Tien", contexts[0])
        self.assertIn("Value: 100", contexts[0])
        self.assertNotIn("Full retrieved document without truncation.", contexts[0])
        self.assertNotIn("Hint:", contexts[0])
        self.assertNotIn("Compact message", contexts[0])

    def test_build_ragas_evaluation_samples(self):
        samples = build_ragas_evaluation_samples(
            [
                {
                    "question": "Doanh thu la bao nhieu?",
                    "answer": "Doanh thu la 100.",
                    "ground_truth": "Doanh thu la 100.",
                    "retrieved_contexts": ["Item: Doanh thu\nValue: 100"],
                }
            ]
        )

        self.assertEqual(
            samples,
            [
                {
                    "user_input": "Doanh thu la bao nhieu?",
                    "response": "Doanh thu la 100.",
                    "retrieved_contexts": ["Item: Doanh thu\nValue: 100"],
                    "reference": "Doanh thu la 100.",
                }
            ],
        )

    def test_merge_predictions_for_records_keeps_seed_order(self):
        records = [
            {"id": 1, "question": "q1"},
            {"id": 2, "question": "q2"},
            {"id": 3, "question": "q3"},
        ]
        merged = merge_predictions_for_records(
            records,
            existing_predictions=[
                {"id": 1, "question": "q1", "answer": "old1"},
                {"id": 3, "question": "q3", "answer": "old3"},
            ],
            new_predictions=[
                {"id": 2, "question": "q2", "answer": "new2"},
            ],
        )

        self.assertEqual([item["id"] for item in merged], [1, 2, 3])
        self.assertEqual([item["answer"] for item in merged], ["old1", "new2", "old3"])

    def test_session_limit_detection(self):
        self.assertTrue(
            is_session_limit_error(
                "ResponseError(you have reached your session usage limit (status code: 429))"
            )
        )
        self.assertFalse(is_session_limit_error("TimeoutError()"))

    def test_session_limit_predictions_are_not_completed_for_resume(self):
        predictions = [
            {"id": 1, "question": "q1", "answer": "ok", "errors": []},
            {
                "id": 2,
                "question": "q2",
                "answer": "Lỗi khi chạy synth: reached your session usage limit (status code: 429)",
                "errors": [],
            },
        ]

        completed = predictions_by_key(predictions, completed_only=True)
        self.assertEqual([item["id"] for item in completed.values()], [1])

    def test_merge_scores_for_predictions_keeps_completed_scores(self):
        predictions = [
            {"id": 1, "question": "q1"},
            {"id": 2, "question": "q2"},
        ]
        merged = merge_scores_for_predictions(
            predictions,
            existing_scores=[
                {"id": 1, "question": "q1", "faithfulness": 0.5},
            ],
            new_scores=[
                {"id": 2, "question": "q2", "faithfulness": 0.7},
            ],
        )

        self.assertEqual([item["id"] for item in merged], [1, 2])
        self.assertEqual([item["faithfulness"] for item in merged], [0.5, 0.7])

    def test_answer_correctness_is_not_a_ragas_metric(self):
        self.assertNotIn("answer_correctness", METRIC_NAMES)

        merged = merge_scores_for_predictions(
            [{"id": 1, "question": "q1"}],
            existing_scores=[
                {
                    "id": 1,
                    "question": "q1",
                    "faithfulness": 0.5,
                    "answer_correctness": 0.9,
                },
            ],
        )

        self.assertNotIn("answer_correctness", merged[0])

    def test_event_loop_closed_error_detection_checks_nested_causes(self):
        root = RuntimeError("Event loop is closed")
        exc = RuntimeError("wrapper")
        exc.__cause__ = root

        self.assertTrue(_is_event_loop_closed_error(exc))
        self.assertFalse(_is_event_loop_closed_error(RuntimeError("TimeoutError")))

    def test_normalized_six_field_factual_recall_contract(self):
        cases = json.loads(
            (ROOT_DIR / "tests" / "fixtures" / "factual_recall_cases.json").read_text(
                encoding="utf-8"
            )
        )

        result = evaluate_factual_recall(cases[:1])

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["recall"], 1.0)
        self.assertEqual(result["explicit_records_n"], 1)
        self.assertEqual(result["ragas_role"], "diagnostic_only")

        missing_reference = evaluate_factual_recall(cases[1:])
        self.assertEqual(missing_reference["status"], "fail")
        self.assertEqual(missing_reference["recall"], 0.0)
        self.assertEqual(
            missing_reference["rows"][0]["missing_facts"][0]["reference"],
            "v17b",
        )

    def test_explicit_expected_facts_are_loaded_from_versioned_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            seed_path = directory_path / "seed.json"
            seed_path.write_text(
                json.dumps(
                    [
                        {
                            "id": 7,
                            "question": "Doanh thu?",
                            "expected_facts": [{"metric": "doanh thu", "value": "10000"}],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            attached = attach_expected_facts_from_seed(
                [{"id": 7, "question": "Doanh thu?", "ground_truth": "10.000"}],
                report={"metadata": {"seed_file": str(seed_path)}},
                report_path=directory_path / "report.json",
            )

        self.assertEqual(attached[0]["expected_facts"][0]["metric"], "doanh thu")

    def test_apec_contract_has_reviewed_six_field_facts(self):
        contract_path = ROOT_DIR / "tests" / "fixtures" / "apec_q211_250_factual_facts.json"
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
        records = attach_expected_facts_from_contract(
            [
                {
                    "id": payload["records"][0]["id"],
                    "question": payload["records"][0]["question"],
                }
            ],
            contract_path,
        )

        expected = records[0]["expected_facts"][0]
        self.assertEqual(
            set(expected),
            {"entity", "metric", "period", "value", "unit", "reference"},
        )

    def test_fact_fields_must_coexist_in_one_retrieved_context(self):
        expected = {
            "entity": "APEC",
            "metric": "Tổng vay ngắn hạn",
            "period": "cuối kỳ",
            "value": "156.502.422.354",
            "unit": "VND",
            "reference": "V.18a",
        }
        actual = facts_from_contexts(
            [
                "Entity: APEC\nMetric: Tổng vay ngắn hạn\nPeriod: cuối kỳ",
                "Value: 156.502.422.354\nUnit: VND\nReference: V.18a",
            ]
        )

        self.assertFalse(any(fact_matches(expected, fact) for fact in actual))

    def test_hard_gate_boundary_is_point_nine_five(self):
        facts = [
            {"metric": f"metric {index}", "value": str(10000 + index)}
            for index in range(20)
        ]
        contexts = [
            f"Metric: metric {index}\nValue: {10000 + index}"
            for index in range(19)
        ]
        record = {
            "id": 1,
            "question": "Tra cứu các số liệu",
            "expected_facts": facts,
            "retrieved_contexts": contexts,
        }

        result = evaluate_factual_recall([record])

        self.assertEqual(FACTUAL_RECALL_THRESHOLD, 0.95)
        self.assertEqual(result["recall"], 0.95)
        self.assertEqual(result["status"], "pass")
        record["retrieved_contexts"] = contexts[:-1]
        self.assertEqual(evaluate_factual_recall([record])["status"], "fail")
        with self.assertRaises(ValueError):
            evaluate_factual_recall([record], threshold=0.94)

    def test_legacy_derived_change_is_not_counted_as_retrieval_source_fact(self):
        result = evaluate_factual_recall(
            [
                {
                    "id": 1,
                    "question": "So sánh lãi cho vay và mức giảm",
                    "ground_truth": (
                        "Năm nay 14.592.618.979 VND, năm trước 26.030.112.902 VND, "
                        "giảm 11.437.493.923 VND."
                    ),
                    "retrieved_contexts": [
                        "Value: 14.592.618.979",
                        "Value: 26.030.112.902",
                    ],
                }
            ]
        )

        self.assertEqual(result["expected_facts_n"], 2)
        self.assertEqual(result["recall"], 1.0)

    def test_normalization_keeps_value_unit_period_and_reference(self):
        self.assertEqual(
            normalize_fact(
                {
                    "entity": "Công ty APEC",
                    "metric": "Phải trả dài hạn khác",
                    "period": "Số cuối kỳ",
                    "value": "(46.440.397.112) VND",
                    "reference": "Thuyết minh V.17b",
                }
            ),
            {
                "entity": "cong ty apec",
                "metric": "phai tra dai han khac",
                "period": "ending",
                "value": "-46440397112",
                "unit": "vnd",
                "reference": "v17b",
            },
        )
        self.assertEqual(normalize_fact({"period": "Năm hiện tại"})["period"], "current_year")
        self.assertEqual(normalize_fact({"period": "Năm trước"})["period"], "prior_year")

    def test_latency_contract_separates_judge_and_invalidates_quota_runs(self):
        predictions = [
            {
                "id": 1,
                "runtime": 1200,
                "tokens": 100,
                "errors": ["retrying with backoff after HTTP 429 quota limit"],
            }
        ]
        contract = build_latency_contract(
            predictions=predictions,
            scores=[],
            eval_error="",
            judge_duration_ms=4500,
        )

        self.assertFalse(contract["valid"])
        self.assertIn("ollama_limit_or_quota_backoff_detected", contract["invalid_reasons"])
        self.assertEqual(contract["prediction"]["p50_ms"], 1200)
        self.assertEqual(contract["judge"]["duration_ms"], 4500)
        self.assertFalse(contract["judge"]["included_in_prediction_latency"])
        self.assertTrue(_latency_contamination_messages(predictions, [], ""))

        clean = build_latency_contract(
            predictions=[{"id": 1, "runtime": 1200, "tokens": 100, "errors": []}],
            scores=[],
            eval_error="",
            judge_duration_ms=10,
            clean_environment_attested=True,
        )
        self.assertTrue(clean["valid"])
        self.assertFalse(clean["baseline_eligible"])
        self.assertFalse(clean["benchmark_cohort"]["matches"])
        unattested = build_latency_contract(
            predictions=[{"id": 1, "runtime": 1200, "tokens": 100, "errors": []}],
            scores=[],
            eval_error="",
            judge_duration_ms=10,
        )
        self.assertFalse(unattested["valid"])
        self.assertIn("clean_environment_not_attested", unattested["invalid_reasons"])

    def test_metric_failure_makes_evaluation_incomplete(self):
        predictions = [{"id": 1, "question": "q1"}]
        scores = [
            {
                "id": 1,
                "question": "q1",
                "faithfulness": 1.0,
                "answer_relevancy_error": "provider unavailable",
            }
        ]

        failures = _metric_failures(predictions, scores)

        self.assertTrue(any("answer_relevancy" in failure for failure in failures))
        self.assertTrue(any("context_precision" in failure for failure in failures))

    def test_schema_v2_contract_and_atomic_report_writes(self):
        predictions = [
            {
                "id": 1,
                "question": "Tra cứu",
                "expected_facts": [{"metric": "doanh thu", "value": "10000"}],
                "retrieved_contexts": ["Metric: doanh thu\nValue: 10000"],
                "runtime": 100,
                "tokens": 10,
                "errors": [],
            }
        ]
        scores = [
            {
                "id": 1,
                "question": "Tra cứu",
                "faithfulness": 1.0,
                "answer_relevancy": 1.0,
                "context_precision": 1.0,
                "context_recall": 1.0,
            }
        ]
        base = {
            "metadata": {"dataset": {"dataset_id": "fixture"}},
            "predictions": predictions,
            "scores": scores,
            "summary": {},
        }
        contracted = apply_evaluation_contract(
            base,
            seed_path=ROOT_DIR / "tests" / "fixtures" / "factual_recall_cases.json",
            predictions=predictions,
            scores=scores,
            eval_error="",
            judge_duration_ms=25,
        )

        self.assertEqual(contracted["schema_version"], 2)
        self.assertEqual(contracted["metadata"]["run_status"], "complete")
        self.assertEqual(contracted["metadata"]["quality_policy"]["ragas"], "diagnostic_only")
        self.assertIsNone(
            contracted["metadata"]["quality_policy"]["ragas_mean_delta_gate"]
        )
        self.assertTrue(contracted["metadata"]["fingerprints"]["seed_sha256"])

        incomplete = apply_evaluation_contract(
            base,
            seed_path=ROOT_DIR / "tests" / "fixtures" / "factual_recall_cases.json",
            predictions=predictions,
            scores=scores,
            eval_error="metric_error (context_recall): provider quota",
            judge_duration_ms=30,
            clean_environment_attested=True,
        )
        self.assertEqual(incomplete["metadata"]["run_status"], "incomplete")
        self.assertFalse(incomplete["metadata"]["run_complete"])
        self.assertFalse(incomplete["summary"]["latency"]["valid"])

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            json_path, csv_path = atomic_write_report_pair(contracted, output)
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["schema_version"], 2)
            self.assertTrue(csv_path.exists())
            self.assertFalse(any(path.name.endswith(".tmp") for path in output.parent.iterdir()))

    def test_embedded_gate_uses_only_reviewed_contract_identities(self):
        unrelated = {
            "id": 1,
            "question": "Tra cứu doanh thu",
            "ground_truth": "10.000",
            "retrieved_contexts": ["Metric: doanh thu\nValue: 10.000"],
            "runtime": 10,
            "tokens": 1,
            "errors": [],
        }
        base = {
            "metadata": {"dataset": {"dataset_id": "apec"}},
            "predictions": [unrelated],
            "scores": [],
            "summary": {},
        }

        contracted = apply_evaluation_contract(
            base,
            seed_path=ROOT_DIR / "dau_tu_APEC_ragas_seed.json",
            predictions=[unrelated],
            scores=[],
            eval_error="",
            judge_duration_ms=0,
        )

        gate = contracted["summary"]["quality_gate"]
        self.assertEqual(gate["status"], "not_evaluated")
        self.assertEqual(gate["expected_facts_n"], 0)
        self.assertEqual(gate["legacy_derived_records_n"], 0)
        self.assertEqual(contracted["metadata"]["run_status"], "complete")

    def test_embedded_gate_records_a_real_strict_pass_and_fail(self):
        passing = self._reviewed_prediction(with_context=True)
        failing = self._reviewed_prediction(with_context=False)
        base = {
            "metadata": {"dataset": {"dataset_id": "apec"}},
            "predictions": [passing],
            "scores": [],
            "summary": {},
        }

        passed = apply_evaluation_contract(
            base,
            seed_path=ROOT_DIR / "dau_tu_APEC_ragas_seed.json",
            predictions=[passing],
            scores=[],
            eval_error="",
            judge_duration_ms=0,
        )
        failed = apply_evaluation_contract(
            base,
            seed_path=ROOT_DIR / "dau_tu_APEC_ragas_seed.json",
            predictions=[failing],
            scores=[],
            eval_error="",
            judge_duration_ms=0,
        )

        self.assertEqual(passed["summary"]["quality_gate"]["status"], "pass")
        self.assertEqual(passed["summary"]["quality_gate"]["legacy_derived_records_n"], 0)
        self.assertEqual(failed["summary"]["quality_gate"]["status"], "fail")
        self.assertEqual(failed["metadata"]["run_status"], "quality_gate_failed")

    def test_source_prediction_incomplete_status_is_preserved(self):
        source = {
            "metadata": {
                "skip_eval": True,
                "run_complete": False,
                "eval_error": "session_limit: interrupted",
            },
            "predictions": [],
            "scores": [],
        }
        complete, source_error = _source_prediction_status(source)
        resumable_judge_checkpoint = {
            "metadata": {"skip_eval": False, "run_complete": False},
            "predictions": [],
            "scores": [],
        }
        judge_complete, _ = _source_prediction_status(resumable_judge_checkpoint)
        contracted = apply_evaluation_contract(
            {"metadata": {"dataset": {"dataset_id": "apec"}}, "summary": {}},
            seed_path=ROOT_DIR / "dau_tu_APEC_ragas_seed.json",
            predictions=[],
            scores=[],
            eval_error="",
            judge_duration_ms=0,
            source_prediction_complete=complete,
            source_prediction_error=source_error,
        )

        self.assertIs(complete, False)
        self.assertIsNone(judge_complete)
        self.assertEqual(contracted["metadata"]["run_status"], "incomplete")
        self.assertFalse(contracted["metadata"]["run_complete"])
        self.assertIn("session_limit", contracted["metadata"]["eval_error"])
        self.assertFalse(contracted["summary"]["latency"]["valid"])

    def test_analyzer_uses_authoritative_contract_and_cli_accepts_path(self):
        passing = self._reviewed_prediction(with_context=True)
        unrelated = {
            "id": 1,
            "question": "Tra cứu doanh thu",
            "ground_truth": "10.000",
            "retrieved_contexts": ["Metric: doanh thu\nValue: 10.000"],
        }
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            report_path.write_text(
                json.dumps(
                    {
                        "metadata": {"dataset": {"dataset_id": "apec"}},
                        "predictions": [unrelated, passing],
                        "scores": [],
                    }
                ),
                encoding="utf-8",
            )
            result = analyze(report_path)
            report_path.write_text(
                json.dumps(
                    {
                        "metadata": {"dataset": {"dataset_id": "apec"}},
                        "predictions": [unrelated],
                        "scores": [],
                    }
                ),
                encoding="utf-8",
            )
            not_evaluated = analyze(report_path)

        self.assertEqual(result["quality_gate"]["status"], "pass")
        self.assertEqual(result["quality_gate"]["expected_facts_n"], 1)
        self.assertEqual(result["quality_gate"]["legacy_derived_records_n"], 0)
        self.assertEqual(not_evaluated["quality_gate"]["status"], "not_evaluated")
        self.assertEqual(not_evaluated["quality_gate"]["legacy_derived_records_n"], 0)
        args = parse_analysis_args(["--facts-contract", "fixture.json", "report.json"])
        self.assertEqual(args.facts_contract, "fixture.json")

    def test_diagnostic_variance_and_three_run_latency_baseline(self):
        first = {
            "run_identity": "run-1",
            "fingerprints": {"config": "same", "predictions_sha256": "run-a"},
            "scores": [{"id": 1, "faithfulness": 0.8}],
            "latency": {
                "valid": True,
                "baseline_eligible": True,
                "prediction": {"p50_ms": 100, "p95_ms": 180, "mean_tokens": 50},
            },
        }
        second = {
            "run_identity": "run-2",
            "fingerprints": {"config": "same", "predictions_sha256": "run-b"},
            "scores": [{"id": 1, "faithfulness": 1.0}],
            "latency": {
                "valid": True,
                "baseline_eligible": True,
                "prediction": {"p50_ms": 110, "p95_ms": 190, "mean_tokens": 55},
            },
        }
        third = {
            "run_identity": "run-3",
            "fingerprints": {"config": "same", "predictions_sha256": "run-c"},
            "scores": [{"id": 1, "faithfulness": 0.9}],
            "latency": {
                "valid": True,
                "baseline_eligible": True,
                "prediction": {"p50_ms": 105, "p95_ms": 185, "mean_tokens": 52},
            },
        }

        variance = summarize_repeated_judge_variance([first, second, third])
        baseline = summarize_clean_latency_baseline([first, second, third])

        self.assertAlmostEqual(variance["faithfulness"]["max_range"], 0.2)
        self.assertEqual(baseline["status"], "established")
        self.assertEqual(baseline["prediction_p50_ms"], 105)
        self.assertEqual(baseline["prediction_p95_ms"], 185)
        self.assertEqual(baseline["mean_tokens"], 52)
        self.assertEqual(
            summarize_clean_latency_baseline([first, first, second])["status"],
            "not_established",
        )


if __name__ == "__main__":
    unittest.main()
