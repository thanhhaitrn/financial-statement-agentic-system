"""Compatibility shim for :mod:`scripts.run_rotate`."""

from scripts.run_rotate import main


if __name__ == "__main__":
    raise SystemExit(main())
