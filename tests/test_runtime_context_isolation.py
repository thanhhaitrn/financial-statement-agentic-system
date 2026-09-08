from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from config.runtime_policy import DEFAULT_POLICY, active_policy, reset_active_policy, set_active_policy
from graph.evidence import _run_evidence_chains_in_plan_order
from graph.state import WorkflowServices
from graph.workflow import _bind_services
from tools.tool_runner import get_collection, reset_collection, set_collection


def test_parallel_graphs_and_worker_threads_keep_policy_and_collection():
    barrier = Barrier(2)

    def run(limit):
        policy = replace(DEFAULT_POLICY, evidence=replace(DEFAULT_POLICY.evidence, max_concurrency=limit))
        def node(_state):
            barrier.wait(timeout=3)
            direct = (active_policy(), get_collection())
            children = _run_evidence_chains_in_plan_order([{}, {}], lambda _: (active_policy(), get_collection()))
            return direct, children
        direct, children = _bind_services(node, WorkflowServices(policy=policy, collection=f"collection-{limit}"))({})
        assert direct == (policy, f"collection-{limit}")
        assert children == [direct, direct]
        assert get_collection() is None
        assert active_policy() == DEFAULT_POLICY

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(run, [2, 3]))


def test_bound_services_restore_context_on_error():
    collection_token = set_collection("outer")
    policy_token = set_active_policy(DEFAULT_POLICY)
    try:
        def fail(_state):
            assert get_collection() is None
            raise ValueError("expected")
        with pytest.raises(ValueError, match="expected"):
            _bind_services(fail, WorkflowServices())({})
        assert get_collection() == "outer"
        assert active_policy() == DEFAULT_POLICY
    finally:
        reset_collection(collection_token)
        reset_active_policy(policy_token)
