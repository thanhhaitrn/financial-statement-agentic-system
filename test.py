"""Compatibility entry point: ``python test.py ...`` runs the pipeline CLI.

The pipeline lives in ``finx_pipeline``. A top-level module named ``test``
collides with Python's standard-library ``test`` package, which wins whenever
the repository root is not first on ``sys.path`` -- e.g. under the installed
``agentfinx`` command, where the web workers run. Library code must import
``finx_pipeline``; this file only keeps the old command working.
"""

import sys

import finx_pipeline as _pipeline

if __name__ == "__main__":
    raise SystemExit(_pipeline.main())

# ``import test`` from the repository root yields the pipeline module itself,
# so attribute patches made through it reach the code that uses them.
sys.modules[__name__] = _pipeline
