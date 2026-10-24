"""Isolated end-to-end tests for supported CLI entrypoints."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType

import pytest

import chat_report
import chat_report.cli as cli
from chat_report.cli import main, strip_deprecated_v1_argv
from chat_report.core.ir import SCHEMA_VERSION
from tests._fixtures import DEFAULT_SESSION_ID, create_synthetic_state_db, create_synthetic_tracking_db


def _args(tmp_path: Path) -> list[str]:
    state = create_synthetic_state_db(tmp_path / "state.vscdb")
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db")
    return ["--state-db", str(state), "--tracking-db", str(tracking), "--workspace", str(tmp_path), "--out", str(tmp_path / "out")]


def test_latest_writes_both_reports_only_under_tmp_path(tmp_path: Path) -> None:
    arguments = _args(tmp_path)
    assert main(["--latest", *arguments, "--agent-digest"]) == 0
    assert (tmp_path / "out" / f"{DEFAULT_SESSION_ID}.json").is_file()
    assert (tmp_path / "out" / f"{DEFAULT_SESSION_ID}.md").is_file()
    assert main(["--latest", *arguments, "--agent-digest"]) == 0


def test_all_modes_and_cli_restrictions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    arguments = _args(tmp_path)
    assert main(["--list-recent", "1", *arguments]) == 0
    assert main([DEFAULT_SESSION_ID, "--chat-id", *arguments]) == 0
    assert main(["request-0", "--request-id", *arguments]) == 0
    assert main(["--latest", *arguments]) == 0
    assert main(["--validate", str(tmp_path / "out" / f"{DEFAULT_SESSION_ID}.json")]) == 0
    assert "conforms to v2.0 (single-session)" in capsys.readouterr().out
    assert main(["--diff", DEFAULT_SESSION_ID, DEFAULT_SESSION_ID, *arguments]) == 0
    assert main(["--aggregate", DEFAULT_SESSION_ID, DEFAULT_SESSION_ID, *arguments]) == 0
    assert main(["--latest", "--format", "md", "--agent-digest", *arguments]) == 0
    assert "digest-forced-both-formats" in capsys.readouterr().err
    assert main(["--latest", "--workspace", str(tmp_path / "missing")]) == 1
    with pytest.raises(SystemExit) as list_error:
        main(["--list-include-subagents", *arguments])
    assert list_error.value.code == 2
    with pytest.raises(SystemExit) as latest_subagents_error:
        main(["--latest", "--list-include-subagents", *arguments])
    assert latest_subagents_error.value.code == 2
    assert main(["--list-recent", "5", "--list-include-subagents", *arguments]) == 0
    with pytest.raises(SystemExit) as latest_error:
        main(["--latest", "--request-id", *arguments])
    assert latest_error.value.code == 2
    with pytest.raises(SystemExit) as digest_error:
        main(["--list-recent", "--agent-digest", *arguments])
    assert digest_error.value.code == 2


def test_deprecated_argument_strip(tmp_path: Path) -> None:
    cleaned, changed = strip_deprecated_v1_argv(["--shape", "wide", "--tools=all", "--latest"])
    assert changed
    assert cleaned == ["--latest"]
    cleaned_adjacent, changed_adjacent = strip_deprecated_v1_argv(["--shape", "--latest", "--tools", "--agent-digest"])
    assert changed_adjacent
    assert cleaned_adjacent == ["--latest", "--agent-digest"]
    res = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[2] / "cursor" / "chat-report.py"),
            "--shape",
            "locked",
            "--tools=all",
            "--latest",
            *_args(tmp_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "deprecated-v1-flag" in res.stderr
    assert main(["--validate", str(tmp_path / "out" / f"{DEFAULT_SESSION_ID}.json")]) == 0


def test_skill_declares_two_stage_dispatch_contract() -> None:
    skill = (Path(__file__).parents[2] / "skills" / "chat-report" / "SKILL.md").read_text(encoding="utf-8")
    assert 'python "<skill_dir>/scripts/run.py"' in skill
    assert "0` subagents" in skill
    for required in (
        "Customization & Harness Conflict Auditor",
        "Root Cause",
        "Conflicting Directives",
        "Proposed Diff",
        "Onboarding Walkthrough Coach",
    ):
        assert required in skill
    assert "python <repo_or_skill_path>/ -m chat_report" not in skill


def test_aggregate_partial_collision_cleans_up_and_increments_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FixedDatetime:
        @classmethod
        def now(cls, timezone: object) -> "FixedDatetime":
            return cls()

        def strftime(self, format_string: str) -> str:
            assert format_string == "%Y%m%dT%H%M%S_%fZ"
            return "20261023T054900_000000Z"

    monkeypatch.setattr(cli, "datetime", FixedDatetime)
    output = tmp_path / "out"
    cohort = hashlib.sha256(f"{DEFAULT_SESSION_ID},{DEFAULT_SESSION_ID}".encode("utf-8")).hexdigest()[:8]
    base = output / f"aggregate-20261023T054900_000000Z-{cohort}"
    output.mkdir()
    base.with_suffix(".md").write_text("collision", encoding="utf-8")
    assert main(["--aggregate", DEFAULT_SESSION_ID, DEFAULT_SESSION_ID, *_args(tmp_path)]) == 0
    assert not base.with_suffix(".json").exists()
    assert (output / f"{base.name}-2.json").is_file()
    assert (output / f"{base.name}-2.md").is_file()


def test_env_db_overrides_and_tracking_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = create_synthetic_state_db(tmp_path / "state.vscdb")
    monkeypatch.setenv("CURSOR_STATE_DB", str(state))
    monkeypatch.setenv("CURSOR_TRACKING_DB", str(tmp_path / "missing_track.db"))
    output = tmp_path / "out"
    assert main(["--latest", "--out", str(output)]) == 1
    monkeypatch.delenv("CURSOR_TRACKING_DB", raising=False)
    monkeypatch.setattr(
        "chat_report.adapters.cursor.paths.default_tracking_db",
        lambda: tmp_path / "absent_default_track.db",
    )
    assert main(["--latest", "--out", str(output)]) == 0
    payload = json.loads((output / f"{DEFAULT_SESSION_ID}.json").read_text(encoding="utf-8"))
    assert any(item["code"] == "cursor-tracking-db-unavailable" for item in payload["warnings"])


def test_state_db_precedence_missing_state_and_default_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = create_synthetic_state_db(tmp_path / "state.vscdb")
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db")
    output = tmp_path / "out"
    monkeypatch.setenv("CURSOR_STATE_DB", str(tmp_path / "missing_env_state.vscdb"))
    assert main(["--latest", "--state-db", str(state), "--tracking-db", str(tracking), "--out", str(output)]) == 0
    assert main(["--latest", "--state-db", str(tmp_path / "missing_explicit_state.vscdb"), "--out", str(output)]) == 1
    monkeypatch.chdir(tmp_path)
    observed_workspaces: list[Path] = []
    classify = cli.classify_session

    def capture_workspace(session: object, workspace: Path) -> object:
        observed_workspaces.append(workspace)
        return classify(session, workspace)

    monkeypatch.setattr(cli, "classify_session", capture_workspace)
    assert main(["--latest", "--state-db", str(state), "--tracking-db", str(tracking), "--out", str(output)]) == 0
    assert observed_workspaces == [tmp_path]


def _load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("sim_chat_report_run", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skill_bundler_overlap_force_rollback_and_isolated_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = Path(__file__).parents[2]
    simulated = tmp_path / "sim_repo"
    shutil.copytree(repository / "chat_report", simulated / "chat_report", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(repository / "skills" / "chat-report", simulated / "skills" / "chat-report", ignore=shutil.ignore_patterns("__pycache__"))
    sim_run = simulated / "skills" / "chat-report" / "scripts" / "run.py"
    for target in (simulated / "skills" / "chat-report", simulated / "chat_report", simulated):
        result = subprocess.run(
            [sys.executable, str(sim_run), "--bundle-into", str(target)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
    installed = simulated / ".cursor" / "skills" / "chat-report"
    external = tmp_path / "ext_bundle"
    for target in (installed, external):
        subprocess.run([sys.executable, str(sim_run), "--bundle-into", str(target)], check=True)
    assert subprocess.run([sys.executable, str(sim_run), "--bundle-into", str(external)]).returncode == 1
    subprocess.run([sys.executable, str(sim_run), "--bundle-into", str(external), "--force"], check=True)
    assert not list(external.parent.glob(f".{external.name}.bak.*"))
    assert not list(external.parent.glob(f".{external.name}.stage.*"))
    marker = external / "marker.txt"
    marker.write_text("preserve", encoding="utf-8")
    module = _load_module(sim_run)
    real_replace = os.replace
    calls = 0

    def fail_second_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated failure")
        real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="simulated failure"):
        module.bundle_into(str(external), force=True)
    assert marker.read_text(encoding="utf-8") == "preserve"

    calls = 0

    def fail_second_and_rollback_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("primary install failure")
        if calls == 3:
            raise OSError("rollback restore failure")
        real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", fail_second_and_rollback_replace)
    with pytest.raises(OSError, match="primary install failure"):
        module.bundle_into(str(external), force=True)
    err = capsys.readouterr().err
    assert "WARNING: bundle rollback failed" in err
    assert "rollback restore failure" in err
    for leftover in external.parent.glob(f".{external.name}.bak.*"):
        real_replace(leftover, external)

    state = create_synthetic_state_db(tmp_path / "state.vscdb")
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db")
    output = tmp_path / "outside"
    output.mkdir()
    subprocess.run(
        [
            sys.executable, str(external / "scripts" / "run.py"), "--latest",
            "--state-db", str(state), "--tracking-db", str(tracking),
            "--out", str(output), "--agent-digest",
        ],
        cwd=output,
        check=True,
    )
    assert main(["--validate", str(output / f"{DEFAULT_SESSION_ID}.json")]) == 0


def test_bundler_arg_variants_quote_recovery_and_illegal_paths(tmp_path: Path) -> None:
    repository = Path(__file__).parents[2]
    run_script = repository / "skills" / "chat-report" / "scripts" / "run.py"
    run_mod = _load_module(run_script)
    dest = tmp_path / "bundle_target"

    assert run_mod.main(["--bundle-into", str(dest)]) == 0
    assert (dest / "SKILL.md").is_file()
    assert run_mod.main(["--bundle-into", str(dest), "--force", "false"]) == 1
    assert run_mod.main([f"--bundle-into={dest}", "--force=false"]) == 1
    assert run_mod.main(["--bundle-into", str(dest), "false"]) == 1

    for force_argv in (
        ["--bundle-into", str(dest), "--force"],
        [f"--bundle-into={dest}", "--force=true"],
        ["--bundle-into", str(dest), "--force", "true"],
        ["--bundle-into", str(dest), "--force", "1"],
        ["--bundle-into", str(dest), "--force=yes"],
        ["--bundle-into", str(dest), "true"],
        ["--bundle-into", str(dest), "force"],
        ["--bundle-into", str(dest), "-f"],
    ):
        assert run_mod.main(force_argv) == 0

    quoted_dest = tmp_path / "quoted_bundle"
    assert run_mod.main(["--bundle-into", f'"{quoted_dest}"']) == 0
    assert (quoted_dest / "SKILL.md").is_file()

    merged_dest = tmp_path / "merged_backslash_bundle"
    merged_arg = f'{merged_dest}\\" --force'
    assert run_mod.main(["--bundle-into", merged_arg]) == 0
    assert (merged_dest / "SKILL.md").is_file()
    assert run_mod.main(["--bundle-into", merged_arg]) == 0
    assert not any("--force" in item.name or '"' in item.name for item in tmp_path.iterdir())

    cli_bundle = tmp_path / "cli_bundle_target"
    assert main(["--bundle-into", str(cli_bundle)]) == 0
    assert (cli_bundle / "SKILL.md").is_file()
    assert main([f"--bundle-into={cli_bundle}", "--force", "true"]) == 0

    for invalid_dest in (
        f'{tmp_path / "bad"}"mid',
        f"{tmp_path / 'bad'}*glob",
        f"{tmp_path / 'bad'}?char",
        f"{tmp_path / 'bad'} --force --latest",
        f"{tmp_path / 'bad'} --force",
        "",
        '""',
    ):
        assert run_mod.main(["--bundle-into", invalid_dest]) == 1

    assert run_mod.main(["--bundle-into", str(dest), "--force=invalid"]) == 1
    assert run_mod.main(["--bundle-into", str(dest), "false", "--force"]) == 1
    assert run_mod.main(["--bundle-into", str(dest), "--latest"]) == 1

    dash_f_dir = tmp_path / "my -f"
    assert run_mod.main(["--bundle-into", str(dash_f_dir)]) == 0
    assert (dash_f_dir / "SKILL.md").is_file()


def test_cli_boolean_flag_values_and_quoted_paths(tmp_path: Path) -> None:
    state = create_synthetic_state_db(tmp_path / "state.vscdb")
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db")
    out_dir = tmp_path / "out_quoted"
    quoted_args = [
        "--state-db", f'"{state}"',
        "--tracking-db", f"'{tracking}'",
        "--workspace", f'"{tmp_path}\\"',
        "--out", f'"{out_dir}"',
    ]
    assert main(["--latest", "true", "--agent-digest=true", "--verbose=0", *quoted_args]) == 0
    report_json = out_dir / f"{DEFAULT_SESSION_ID}.json"
    assert report_json.is_file()
    assert main(["--validate", f'"{report_json}"']) == 0
    assert main(["--no-subagents", DEFAULT_SESSION_ID, *quoted_args]) == 0
    assert main(["--no-subagents", "false", DEFAULT_SESSION_ID, *quoted_args]) == 0
    assert main(["--request-id", "true", "request-0", *quoted_args]) == 0
    assert main(["--latest=false", DEFAULT_SESSION_ID, *quoted_args]) == 0


def test_version_constants_and_cli_version_flags() -> None:
    assert chat_report.__version__ == "2.0.1"
    assert chat_report.SCHEMA_VERSION == "2.0"
    assert SCHEMA_VERSION == "2.0"
    repository = Path(__file__).parents[2]
    entrypoints = (
        [sys.executable, "-m", "chat_report"],
        [sys.executable, str(repository / "cursor" / "chat-report.py")],
        [sys.executable, str(repository / "skills" / "chat-report" / "scripts" / "run.py")],
    )
    for command in entrypoints:
        for flag in ("--version", "-V"):
            result = subprocess.run(
                [*command, flag],
                cwd=repository,
                capture_output=True,
                text=True,
                check=True,
            )
            assert result.returncode == 0
            assert result.stdout.strip() == "chat-report 2.0.1 (schema 2.0)"

