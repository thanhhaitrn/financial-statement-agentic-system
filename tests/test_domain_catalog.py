"""Domain wording lives in one versioned catalog."""

from config.domain_catalog import (
    ANALYSIS_AXIS_ALIASES,
    CURRENT_PERIOD_MARKERS,
    DOMAIN_CATALOG_VERSION,
    PREVIOUS_PERIOD_MARKERS,
    TABLE_CANON,
    catalog_fingerprint,
)


def test_schema_and_synth_read_the_catalog_rather_than_their_own_copy():
    import agents.synth_runner as synth_runner
    import schemas.agent_outputs as agent_outputs

    assert agent_outputs.TABLE_CANON is TABLE_CANON
    assert agent_outputs.ANALYSIS_AXIS_ALIASES is ANALYSIS_AXIS_ALIASES
    assert synth_runner._CURRENT_PERIOD_MARKERS is CURRENT_PERIOD_MARKERS
    assert synth_runner._PREVIOUS_PERIOD_MARKERS is PREVIOUS_PERIOD_MARKERS


def test_axis_aliases_resolve_to_registered_agents_only():
    from agents.agent_registry import ANALYSIS_AGENT_ORDER

    assert set(ANALYSIS_AXIS_ALIASES.values()) == set(ANALYSIS_AGENT_ORDER)


def test_table_aliases_resolve_to_canonical_table_names():
    from schemas.agent_outputs import VALID_TABLE_NAMES

    assert set(TABLE_CANON.values()) == set(VALID_TABLE_NAMES)


def test_period_markers_do_not_overlap():
    assert not set(CURRENT_PERIOD_MARKERS) & set(PREVIOUS_PERIOD_MARKERS)


def test_fingerprint_is_stable_and_versioned():
    assert DOMAIN_CATALOG_VERSION.startswith("agentfinx-domain-catalog-")
    assert catalog_fingerprint() == catalog_fingerprint()
    assert len(catalog_fingerprint()) == 16
