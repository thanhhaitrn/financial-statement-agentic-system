"""External evidence providers used by the deterministic evidence stage."""

import atexit
from collections import OrderedDict
from pathlib import Path
from threading import Lock

from acquisition.quota import AcquisitionQuota
from acquisition.store import AcquisitionStore
from config.runtime_policy import RuntimePolicy
from dataset_catalog.registry import DATASETS_DIR
from web_evidence.cache import WebEvidenceCache
from web_evidence.vietstock import VietstockHttpFetcher, VietstockNewsProvider


_providers = OrderedDict()
_providers_lock = Lock()


def close_default_web_providers():
    with _providers_lock:
        providers = list(_providers.values())
        _providers.clear()
    for provider in providers:
        provider.close(wait=False)


atexit.register(close_default_web_providers)


def build_default_web_provider(policy: RuntimePolicy):
    if not policy.web.enabled:
        return None
    with _providers_lock:
        if policy in _providers:
            _providers.move_to_end(policy)
            return _providers[policy]
        provider = _create_web_provider(policy)
        _providers[policy] = provider
        if len(_providers) > 8:
            _, old = _providers.popitem(last=False)
            old.close(wait=False)
        return provider


def _create_web_provider(policy: RuntimePolicy):
    cache_path = Path(DATASETS_DIR) / "web" / "evidence.db"
    quota = AcquisitionQuota(
        AcquisitionStore(Path(DATASETS_DIR) / "acquisition" / "acquisition.db"),
        policy.acquisition,
    )
    return VietstockNewsProvider(
        cache=WebEvidenceCache(cache_path),
        fetcher=VietstockHttpFetcher(
            timeout_seconds=policy.web.request_timeout_seconds,
            max_body_bytes=policy.web.max_body_bytes,
        ),
        cache_ttl_seconds=policy.web.cache_ttl_seconds,
        miss_wait_seconds=policy.web.miss_wait_seconds,
        max_refresh_articles=policy.web.max_refresh_articles,
        refresh_concurrency=policy.web.refresh_concurrency,
        proactive_refresh_admitter=quota.admit_news_refresh,
    )


__all__ = ["VietstockNewsProvider", "build_default_web_provider"]
