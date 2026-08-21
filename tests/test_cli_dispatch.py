"""The unified ``agentfinx`` CLI must route each subcommand to the right module."""

import sys

import agentfinx.cli as cli


def test_unknown_command_returns_usage_error(capsys):
    assert cli.main(["frobnicate"]) == 2
    assert "unknown command" in capsys.readouterr().err


def test_no_args_prints_usage(capsys):
    assert cli.main([]) == 2
    assert "usage: agentfinx" in capsys.readouterr().out


def test_argv_aware_command_receives_remaining_args(monkeypatch):
    seen = {}

    def fake_main(argv=None):
        seen["argv"] = argv
        return 0

    import ragas_eval_runner

    monkeypatch.setattr(ragas_eval_runner, "main", fake_main)
    assert cli.main(["score", "--predictions-file", "x.json"]) == 0
    assert seen["argv"] == ["--predictions-file", "x.json"]


def test_no_argv_command_rebuilds_sys_argv(monkeypatch):
    seen = {}

    def fake_main():
        seen["argv"] = list(sys.argv)
        return 0

    import test as legacy_cli

    monkeypatch.setattr(legacy_cli, "main", fake_main)
    original = list(sys.argv)
    assert cli.main(["ask", "--query", "hello"]) == 0
    assert seen["argv"] == ["agentfinx ask", "--query", "hello"]
    assert sys.argv == original  # restored after dispatch
