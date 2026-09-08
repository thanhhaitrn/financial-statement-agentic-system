"""A parser behaviour change must bump the contract version."""

from ingestion.pipeline import (
    PARSER_CONTRACT_SOURCE_FILES,
    PARSER_CONTRACT_SOURCE_SHA256,
    PARSER_CONTRACT_VERSION,
    parser_contract_source_sha256,
)


def test_parser_source_digest_is_pinned_to_the_contract_version():
    assert parser_contract_source_sha256() == PARSER_CONTRACT_SOURCE_SHA256, (
        "An ingestion parser changed. Facts extracted before and after are no "
        "longer comparable, so bump PARSER_CONTRACT_VERSION (currently "
        f"{PARSER_CONTRACT_VERSION!r}) and update PARSER_CONTRACT_SOURCE_SHA256 "
        "in ingestion/pipeline.py in the same commit."
    )


def test_every_pinned_parser_module_exists():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for relative_path in PARSER_CONTRACT_SOURCE_FILES:
        assert (root / relative_path).is_file(), relative_path
