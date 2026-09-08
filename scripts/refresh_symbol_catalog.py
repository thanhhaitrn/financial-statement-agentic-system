"""Daily worker entry point for the CafeF ticker/company catalog."""

from __future__ import annotations

import argparse
from pathlib import Path

from config.runtime_policy import DEFAULT_POLICY
from dataset_catalog.registry import DATASETS_DIR
from web_evidence.symbols import CafeFSymbolCatalogJob


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(Path(DATASETS_DIR) / "web" / "cafef_symbols.json"),
    )
    args = parser.parse_args()
    payload = CafeFSymbolCatalogJob(
        timeout_seconds=DEFAULT_POLICY.acquisition.download_timeout_seconds,
    ).refresh(args.output)
    print(f"updated {len(payload['symbols'])} symbols -> {args.output}")


if __name__ == "__main__":
    main()
