"""Daily worker entry point for the CafeF ticker/company catalog."""

from __future__ import annotations

import argparse

from agentfinx.core.config import paths
from agentfinx.core.config.runtime_policy import DEFAULT_POLICY
from agentfinx.acquisition.symbols import CafeFSymbolCatalogJob


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=None,
        help="Catalog file (default: storage/catalog/cafef_symbols.json).",
    )
    args = parser.parse_args()
    args.output = args.output or str(paths.symbol_catalog_path())
    payload = CafeFSymbolCatalogJob(
        timeout_seconds=DEFAULT_POLICY.acquisition.download_timeout_seconds,
    ).refresh(args.output)
    print(f"updated {len(payload['symbols'])} symbols -> {args.output}")


if __name__ == "__main__":
    main()
