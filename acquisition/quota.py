"""Admission control for trial accounts and the global LlamaParse budget."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable
from functools import wraps

from acquisition.store import AcquisitionStore
from config.runtime_policy import AcquisitionPolicy


class QuotaExceeded(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _atomic_admission(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.store.transaction():
            return method(self, *args, **kwargs)
    return wrapped


class AcquisitionQuota:
    def __init__(
        self,
        store: AcquisitionStore,
        policy: AcquisitionPolicy,
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self.store = store
        self.policy = policy
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _current_time(self) -> datetime:
        current = self._now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)

    def _check_trial_period(self, owner_id: str) -> None:
        current = self._current_time()
        started_raw = self.store.ensure_trial(
            owner_id,
            started_at=current.replace(microsecond=0).isoformat(),
        )
        started = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if current >= started + timedelta(days=self.policy.trial_days):
            raise QuotaExceeded("trial_expired", "The trial period has expired")

    def budget_state(self, *, additional_credits: int = 0) -> dict:
        budget = self.policy.monthly_parse_credit_budget
        month_start = self._current_time().strftime("%Y-%m-01T00:00:00+00:00")
        committed = self.store.committed_credits(since=month_start)
        projected = committed + max(0, int(additional_credits))
        ratio = projected / budget if budget else 1.0
        return {
            "budget": budget,
            "committed": committed,
            "projected": projected,
            "ratio": ratio,
            "warning": ratio >= self.policy.budget_warning_percent,
            "trial_locked": ratio >= self.policy.budget_trial_lock_percent,
            "provider_locked": ratio >= 1.0,
        }

    def check_import_slot(self, owner_id: str) -> None:
        self._check_trial_period(owner_id)
        successful = self.store.count_jobs(owner_id, statuses={"ready"})
        if successful >= self.policy.trial_successful_imports:
            raise QuotaExceeded("trial_import_limit", "Trial report import limit reached")
        active_statuses = {
            "queued",
            "downloading",
            "parsing",
            "validating",
            "building",
        }
        active = (self.store.count_jobs(owner_id, statuses=active_statuses)
                  + self.store.count_unreconciled_jobs(owner_id))
        if active >= self.policy.per_owner_concurrency:
            raise QuotaExceeded("owner_concurrency", "An import is already active for this account")

    @staticmethod
    def estimate_import_credits(page_count: int) -> int:
        # Cost-effective parse (3) plus one targeted Agentic retry (10).
        return max(0, int(page_count)) * (3 + 10)

    def reserve_import(self, owner_id: str, *, page_count: int) -> int:
        self._check_trial_period(owner_id)
        if page_count > self.policy.max_pdf_pages:
            raise QuotaExceeded("too_many_pages", "PDF exceeds the trial page limit")
        # Reserve the worst allowed path: cost_effective plus one Agentic retry
        # for every page. Reconciliation releases the unused portion.
        reserved = self.estimate_import_credits(page_count)
        budget_state = self.budget_state(additional_credits=reserved)
        if budget_state["provider_locked"]:
            raise QuotaExceeded(
                "global_provider_lock",
                "The LlamaParse budget is exhausted",
            )
        if budget_state["trial_locked"]:
            raise QuotaExceeded("global_trial_lock", "Trial parsing budget is temporarily exhausted")
        return reserved

    @_atomic_admission
    def admit_question(self, owner_id: str) -> None:
        self._check_trial_period(owner_id)
        day_start = self._current_time().strftime("%Y-%m-%dT00:00:00+00:00")
        if self.store.usage_total(owner_id, kind="question") >= self.policy.trial_questions_total:
            raise QuotaExceeded("trial_question_total", "Trial question limit reached")
        if (
            self.store.usage_total(owner_id, kind="question", since=day_start)
            >= self.policy.trial_questions_per_day
        ):
            raise QuotaExceeded("trial_question_daily", "Daily question limit reached")
        self.store.record_usage(owner_id, kind="question")

    @_atomic_admission
    def admit_news_refresh(self, owner_id: str) -> None:
        self._check_trial_period(owner_id)
        day_start = self._current_time().strftime("%Y-%m-%dT00:00:00+00:00")
        if (
            self.store.usage_total(owner_id, kind="news_refresh", since=day_start)
            >= self.policy.trial_news_refreshes_per_day
        ):
            raise QuotaExceeded("trial_news_refresh_daily", "Daily news refresh limit reached")
        self.store.record_usage(owner_id, kind="news_refresh")
