"""The CLI end to end: analyze -> validate on a small repo, with the fake model and no network."""

import re
import sys

import pytest
from typer.testing import CliRunner

from codebase_analyzer import cli

from ..fakes import FakeStructuredModel, fake_provider

CONTROLLER = "com/acme/shop/order/controller/OrderController.java"


@pytest.fixture
def workdir(tmp_path_factory, monkeypatch):
    """Run from an empty directory: no .env is picked up and the cache stays out of the repo."""
    work = tmp_path_factory.mktemp("work")
    monkeypatch.chdir(work)
    return work


def test_analyze_then_validate(make_repo, fixture_source, workdir, monkeypatch):
    repo = make_repo({CONTROLLER: fixture_source("OrderController.java")}, context_path="/api")
    monkeypatch.setattr(cli, "make_provider", lambda settings: fake_provider(FakeStructuredModel()))
    runner = CliRunner()

    analyzed = runner.invoke(cli.app, ["analyze", "--repo", str(repo), "--output", "out.json", "--no-verbose"])
    assert analyzed.exit_code == 0, analyzed.output
    assert re.search(r"Methods: (\d+) total, \1 described", analyzed.output)  # every method described
    assert (workdir / "out.json").exists()

    validated = runner.invoke(cli.app, ["validate", "out.json", "--repo", str(repo)])
    assert validated.exit_code == 0, validated.output
    assert "coverage OK" in validated.output

    replayed = runner.invoke(cli.app, ["analyze", "--repo", str(repo), "--output", "again.json", "--no-verbose"])
    assert "LLM calls: 0" in replayed.output  # everything comes from the response cache the first run wrote


@pytest.mark.parametrize(
    ("argv", "env", "expected"),
    [
        (["estimate", "--repo", "."], {}, "has no src/main/java directory"),
        (["estimate", "--repo", "{repo}"], {"LLM_PROVIDER": "nope"}, "invalid configuration (LLM_PROVIDER"),
    ],
)
def test_configuration_errors_are_one_line_messages(
    argv, env, expected, make_repo, fixture_source, workdir, monkeypatch, capsys
):
    repo = make_repo({CONTROLLER: fixture_source("OrderController.java")})
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", ["codebase-analyzer", *(a.format(repo=repo) for a in argv)])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    err = capsys.readouterr().err
    assert exit_info.value.code == 2
    assert err.startswith("error: ") and expected in err and "Traceback" not in err
