"""Typed runtime limits for one workflow run.

Every execution cap the graph honours lives here as a frozen policy group, is
parsed from the environment exactly once, and is validated strictly: an invalid
override fails at startup instead of silently falling back to a default and
producing a run nobody can reproduce.

The policy is injected through ``WorkflowServices`` and activated for the
execution context by ``set_active_policy``; modules read ``active_policy()`` instead of
holding their own copy of a constant or calling ``os.getenv`` themselves.
"""
# Code note: Config modules centralize constants used by routing, ingestion, and retrieval.

from __future__ import annotations

import json
import os
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from typing import Mapping, Optional

ROUTING_MODES = ("legacy", "shadow", "model_first")


class RuntimePolicyError(ValueError):
    """An environment override is present but not usable."""


def _int_env(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: Optional[int] = None,
) -> int:
    raw = str(env.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimePolicyError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise RuntimePolicyError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise RuntimePolicyError(f"{name} must be <= {maximum}, got {value}")
    return value


def _float_env(
    env: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = str(env.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimePolicyError(f"{name} must be a number, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise RuntimePolicyError(
            f"{name} must be within [{minimum}, {maximum}], got {value}"
        )
    return value


def _bool_env(
    env: Mapping[str, str],
    name: str,
    default: Optional[bool],
) -> Optional[bool]:
    raw = str(env.get(name, "") or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise RuntimePolicyError(f"{name} must be a boolean (0/1), got {raw!r}")


def _choice_env(
    env: Mapping[str, str],
    name: str,
    default: str,
    choices: tuple[str, ...],
) -> str:
    raw = str(env.get(name, "") or "").strip().lower()
    if not raw:
        return default
    if raw not in choices:
        raise RuntimePolicyError(f"{name} must be one of {list(choices)}, got {raw!r}")
    return raw


@dataclass(frozen=True)
class ExecutionPolicy:
    """How many turns each agent gets before the graph moves on."""

    max_tool_calls_per_round: int = 2
    # Synth and the graph edge must agree, otherwise the graph keeps routing
    # follow-ups that Synth already refuses to emit.
    max_followup_rounds: int = 2
    max_followup_requirements: int = 10
    max_target_requirements: int = 8


@dataclass(frozen=True)
class SynthPolicy:
    """Facts handed to the synthesizer and how many repair rounds it may use."""

    max_facts_per_agent: int = 20
    max_general_repairs: int = 1
    max_targeted_repairs: int = 1


@dataclass(frozen=True)
class EvidencePolicy:
    """Per-route fact caps and retrieval concurrency."""

    default_facts_limit: int = 10
    note_facts_limit: int = 12
    schedule_facts_limit: int = 24
    schedule_main_facts_limit: int = 16
    report_section_facts_limit: int = 10
    # One cap from retrieval all the way to the analysis prompt: a lower clamp
    # further down means facts are fetched, paid for, then dropped unseen.
    hard_analysis_main_facts_limit: int = 32
    note_ref_scan_limit: int = 15
    note_ref_enrichment_limit: int = 8
    max_concurrency: int = 4


@dataclass(frozen=True)
class WebPolicy:
    """External evidence freshness and bounded refresh behaviour."""

    enabled: bool = False
    cache_ttl_seconds: int = 6 * 60 * 60
    miss_wait_seconds: int = 10
    max_results: int = 10
    max_refresh_articles: int = 20
    request_timeout_seconds: int = 15
    max_body_bytes: int = 50_000
    refresh_concurrency: int = 1


@dataclass(frozen=True)
class AcquisitionPolicy:
    """Admission limits for report discovery, download, and conversion."""

    max_file_bytes: int = 25 * 1024 * 1024
    max_pdf_pages: int = 200
    candidate_ttl_seconds: int = 30 * 60
    trial_days: int = 14
    trial_successful_imports: int = 1
    trial_questions_per_day: int = 20
    trial_questions_total: int = 100
    trial_news_refreshes_per_day: int = 3
    per_owner_concurrency: int = 1
    global_parse_concurrency: int = 2
    provider_max_retries: int = 3
    provider_circuit_failure_threshold: int = 3
    provider_circuit_cooldown_seconds: int = 60
    download_timeout_seconds: int = 30
    llamaparse_tier: str = "cost_effective"
    llamaparse_version: str = "2026-08-19"
    monthly_parse_credit_budget: int = 10_000
    budget_warning_percent: float = 0.80
    budget_trial_lock_percent: float = 0.95


@dataclass(frozen=True)
class ObservabilityPolicy:
    """Preview and trace-size ceilings; these never change answer content."""

    value_preview_limit: int = 220
    hint_preview_limit: int = 180
    tool_context_preview_limit: int = 1200
    max_event_bytes: int = 4096
    debug_max_event_bytes: int = 32768


@dataclass(frozen=True)
class RoutingPolicy:
    """Semantic-routing rollout. Thresholds apply to the fallback path only.

    ``mode`` sets the default for the two gates that let a heuristic *override*
    a decision the model already made. The third gate is retrieval recall, not
    a semantic override, so it is a separate switch: an A/B on 2026-09-03
    (suavietnam, "Đánh giá tình hình tài chính") measured that disabling it cut
    cash-flow facts from 39 to 1 and made the answer claim CFI/CFF were missing
    when both are in the report. It therefore stays on under model_first.
    """

    mode: str = "model_first"
    fallback_confidence_threshold: float = 0.60
    # None = follow ``mode``; True/False pins the gate regardless of mode.
    planner_axis_expansion: Optional[bool] = None
    router_direct_bypass: Optional[bool] = None
    # Recall-first retrieval augmentation. Independent of ``mode`` by default.
    evidence_augmentation: bool = True

    def _mode_allows_override(self) -> bool:
        return self.mode != "model_first"

    @property
    def planner_axis_expansion_enabled(self) -> bool:
        if self.planner_axis_expansion is None:
            return self._mode_allows_override()
        return bool(self.planner_axis_expansion)

    @property
    def router_direct_bypass_enabled(self) -> bool:
        if self.router_direct_bypass is None:
            return self._mode_allows_override()
        return bool(self.router_direct_bypass)

    @property
    def evidence_augmentation_enabled(self) -> bool:
        return bool(self.evidence_augmentation)


@dataclass(frozen=True)
class RuntimePolicy:
    execution: ExecutionPolicy = ExecutionPolicy()
    synth: SynthPolicy = SynthPolicy()
    evidence: EvidencePolicy = EvidencePolicy()
    web: WebPolicy = WebPolicy()
    acquisition: AcquisitionPolicy = AcquisitionPolicy()
    observability: ObservabilityPolicy = ObservabilityPolicy()
    routing: RoutingPolicy = RoutingPolicy()

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "RuntimePolicy":
        source: Mapping[str, str] = os.environ if env is None else env
        return cls(
            execution=ExecutionPolicy(
                max_tool_calls_per_round=_int_env(
                    source, "MAX_TOOL_CALLS_PER_ROUND", 2, maximum=10
                ),
                max_followup_rounds=_int_env(
                    source, "MAX_FOLLOWUP_ROUNDS", 2, minimum=0, maximum=5
                ),
                max_followup_requirements=_int_env(
                    source, "MAX_FOLLOWUP_REQUIREMENTS", 10, maximum=50
                ),
                max_target_requirements=_int_env(
                    source, "MAX_TARGET_REQUIREMENTS", 8, maximum=50
                ),
            ),
            synth=SynthPolicy(
                max_facts_per_agent=_int_env(
                    source, "MAX_SYNTH_FACTS_PER_AGENT", 20, maximum=200
                ),
                max_general_repairs=_int_env(
                    source, "SYNTH_MAX_GENERAL_REPAIRS", 1, minimum=0, maximum=3
                ),
                max_targeted_repairs=_int_env(
                    source, "SYNTH_MAX_TARGETED_REPAIRS", 1, minimum=0, maximum=3
                ),
            ),
            evidence=EvidencePolicy(
                default_facts_limit=_int_env(
                    source, "EVIDENCE_FACTS_LIMIT", 10, maximum=200
                ),
                note_facts_limit=_int_env(source, "NOTE_FACTS_LIMIT", 12, maximum=200),
                schedule_facts_limit=_int_env(
                    source, "SCHEDULE_FACTS_LIMIT", 24, maximum=200
                ),
                schedule_main_facts_limit=_int_env(
                    source, "SCHEDULE_MAIN_FACTS_LIMIT", 16, maximum=200
                ),
                report_section_facts_limit=_int_env(
                    source, "REPORT_SECTION_FACTS_LIMIT", 10, maximum=200
                ),
                hard_analysis_main_facts_limit=_int_env(
                    source, "HARD_ANALYSIS_MAIN_FACTS_LIMIT", 32, maximum=200
                ),
                note_ref_scan_limit=_int_env(
                    source, "NOTE_REF_FACTS_SCAN_LIMIT", 15, maximum=200
                ),
                note_ref_enrichment_limit=_int_env(
                    source, "NOTE_REF_ENRICHMENT_LIMIT", 8, maximum=200
                ),
                max_concurrency=_int_env(
                    source, "EVIDENCE_MAX_CONCURRENCY", 4, maximum=4
                ),
            ),
            web=WebPolicy(
                enabled=bool(_bool_env(source, "WEB_EVIDENCE_ENABLED", False)),
                cache_ttl_seconds=_int_env(
                    source, "WEB_CACHE_TTL_SECONDS", 6 * 60 * 60, maximum=7 * 24 * 60 * 60
                ),
                miss_wait_seconds=_int_env(
                    source, "WEB_MISS_WAIT_SECONDS", 10, maximum=60
                ),
                max_results=_int_env(source, "WEB_MAX_RESULTS", 10, maximum=50),
                max_refresh_articles=_int_env(
                    source, "WEB_MAX_REFRESH_ARTICLES", 20, maximum=200
                ),
                request_timeout_seconds=_int_env(
                    source, "WEB_REQUEST_TIMEOUT_SECONDS", 15, maximum=60
                ),
                max_body_bytes=_int_env(
                    source, "WEB_MAX_BODY_BYTES", 50_000, maximum=1_000_000
                ),
                refresh_concurrency=_int_env(
                    source, "WEB_REFRESH_CONCURRENCY", 1, maximum=4
                ),
            ),
            acquisition=AcquisitionPolicy(
                max_file_bytes=_int_env(
                    source, "REPORT_MAX_FILE_BYTES", 25 * 1024 * 1024, maximum=100 * 1024 * 1024
                ),
                max_pdf_pages=_int_env(
                    source, "REPORT_MAX_PDF_PAGES", 200, maximum=2_000
                ),
                candidate_ttl_seconds=_int_env(
                    source, "REPORT_CANDIDATE_TTL_SECONDS", 30 * 60, maximum=24 * 60 * 60
                ),
                trial_days=_int_env(source, "TRIAL_DAYS", 14, maximum=365),
                trial_successful_imports=_int_env(
                    source, "TRIAL_SUCCESSFUL_IMPORTS", 1, maximum=100
                ),
                trial_questions_per_day=_int_env(
                    source, "TRIAL_QUESTIONS_PER_DAY", 20, maximum=10_000
                ),
                trial_questions_total=_int_env(
                    source, "TRIAL_QUESTIONS_TOTAL", 100, maximum=100_000
                ),
                trial_news_refreshes_per_day=_int_env(
                    source, "TRIAL_NEWS_REFRESHES_PER_DAY", 3, maximum=1_000
                ),
                per_owner_concurrency=_int_env(
                    source, "REPORT_PER_OWNER_CONCURRENCY", 1, maximum=10
                ),
                global_parse_concurrency=_int_env(
                    source, "REPORT_GLOBAL_PARSE_CONCURRENCY", 2, maximum=5
                ),
                provider_max_retries=_int_env(
                    source, "REPORT_PROVIDER_MAX_RETRIES", 3, maximum=10
                ),
                provider_circuit_failure_threshold=_int_env(
                    source,
                    "REPORT_PROVIDER_CIRCUIT_FAILURE_THRESHOLD",
                    3,
                    maximum=20,
                ),
                provider_circuit_cooldown_seconds=_int_env(
                    source,
                    "REPORT_PROVIDER_CIRCUIT_COOLDOWN_SECONDS",
                    60,
                    maximum=3_600,
                ),
                download_timeout_seconds=_int_env(
                    source, "REPORT_DOWNLOAD_TIMEOUT_SECONDS", 30, maximum=300
                ),
                llamaparse_tier=_choice_env(
                    source,
                    "LLAMAPARSE_TIER",
                    "cost_effective",
                    ("cost_effective", "agentic"),
                ),
                llamaparse_version=(
                    str(source.get("LLAMAPARSE_VERSION", "") or "").strip()
                    or "2026-08-19"
                ),
                monthly_parse_credit_budget=_int_env(
                    source, "REPORT_MONTHLY_PARSE_CREDIT_BUDGET", 10_000, maximum=100_000_000
                ),
                budget_warning_percent=_float_env(
                    source, "REPORT_BUDGET_WARNING_PERCENT", 0.80, minimum=0.0, maximum=1.0
                ),
                budget_trial_lock_percent=_float_env(
                    source, "REPORT_BUDGET_TRIAL_LOCK_PERCENT", 0.95, minimum=0.0, maximum=1.0
                ),
            ),
            observability=ObservabilityPolicy(
                value_preview_limit=_int_env(
                    source, "EVIDENCE_VALUE_PREVIEW_LIMIT", 220, maximum=10000
                ),
                hint_preview_limit=_int_env(
                    source, "EVIDENCE_HINT_PREVIEW_LIMIT", 180, maximum=10000
                ),
                tool_context_preview_limit=_int_env(
                    source, "TOOL_CONTEXT_PREVIEW_LIMIT", 1200, maximum=100000
                ),
                max_event_bytes=_int_env(
                    source, "TRACE_MAX_EVENT_BYTES", 4096, maximum=1000000
                ),
                debug_max_event_bytes=_int_env(
                    source, "TRACE_DEBUG_MAX_EVENT_BYTES", 32768, maximum=1000000
                ),
            ),
            routing=RoutingPolicy(
                mode=_choice_env(source, "ROUTING_MODE", "model_first", ROUTING_MODES),
                fallback_confidence_threshold=_float_env(
                    source,
                    "ROUTING_FALLBACK_CONFIDENCE",
                    0.60,
                    minimum=0.0,
                    maximum=1.0,
                ),
                planner_axis_expansion=_bool_env(
                    source, "ROUTING_PLANNER_AXIS_EXPANSION", None
                ),
                router_direct_bypass=_bool_env(
                    source, "ROUTING_DIRECT_BYPASS", None
                ),
                evidence_augmentation=bool(
                    _bool_env(source, "ROUTING_EVIDENCE_AUGMENTATION", True)
                ),
            ),
        )

    def as_dict(self) -> dict:
        return asdict(self)

    def fingerprint(self) -> str:
        """Stable digest so a resumed batch never mixes two policies."""

        payload = json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=False)
        return sha256(payload.encode("utf-8")).hexdigest()[:16]

    def with_overrides(self, **groups) -> "RuntimePolicy":
        return replace(self, **groups)


DEFAULT_POLICY = RuntimePolicy.from_env()

_active_policy: ContextVar[RuntimePolicy] = ContextVar("agentfinx_policy", default=DEFAULT_POLICY)


def set_active_policy(policy: Optional[RuntimePolicy]) -> Token:
    """Activate a context-local policy; callers may reset using the returned token."""
    return _active_policy.set(policy or DEFAULT_POLICY)


def reset_active_policy(token: Token) -> None:
    _active_policy.reset(token)


def active_policy() -> RuntimePolicy:
    return _active_policy.get()


def routing_mode() -> str:
    return active_policy().routing.mode


def model_first_routing() -> bool:
    """True when a successful model decision must not be overridden by heuristics."""

    return routing_mode() == "model_first"


def planner_axis_expansion_enabled() -> bool:
    """May a heuristic add analysis axes the planner model did not choose?"""

    return active_policy().routing.planner_axis_expansion_enabled


def router_direct_bypass_enabled() -> bool:
    """May a deterministic plan answer without calling the router model?"""

    return active_policy().routing.router_direct_bypass_enabled


def evidence_augmentation_enabled() -> bool:
    """May retrieval add tables alongside the ones the router named?

    This is recall, not a semantic override: it never removes or replaces the
    router's choice, it only fetches the main-report line the query also names.
    """

    return active_policy().routing.evidence_augmentation_enabled


def shadow_routing() -> bool:
    """True when legacy behaviour runs but the model-first difference is traced."""

    return routing_mode() == "shadow"
