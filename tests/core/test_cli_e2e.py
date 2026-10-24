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
import chat_report.installer as installer
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
    assert chat_report.__version__ == "2.0.2"
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
            assert result.stdout.strip() == "chat-report 2.0.2 (schema 2.0)"


def test_skill_assets_sync_and_lightweight_rendering() -> None:
    repository = Path(__file__).parents[2]
    repo_skill = (repository / "skills" / "chat-report" / "SKILL.md").read_text(encoding="utf-8")
    packaged_skill = (repository / "chat_report" / "assets" / "SKILL.md").read_text(encoding="utf-8")
    assert repo_skill == packaged_skill
    assert repo_skill.count('python "<skill_dir>/scripts/run.py"') == 5
    lightweight = installer.render_lightweight_skill_md()
    assert "<skill_dir>/scripts/run.py" not in lightweight
    for expected_cmd in (
        "chat-report --latest --agent-digest",
        "chat-report --list-recent 5",
        "chat-report <uuid> --agent-digest",
        "chat-report --diff <before_uuid> <after_uuid>",
        "chat-report --aggregate <uuid_1> <uuid_2>",
    ):
        assert expected_cmd in lightweight


def test_global_install_shims_path_diagnostics_and_skill_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    runtime_dir = tmp_path / "rt space ' $ %VAR% dir"
    bin_dir = tmp_path / "bin_dir"
    monkeypatch.setenv("PATH", str(tmp_path / "other_path"))

    assert main(["--install", "--runtime-dir", str(runtime_dir), "--bin-dir", str(bin_dir)]) == 0
    err = capsys.readouterr().err
    assert "bin-dir-not-on-path" in err

    user_skill_dir = fake_home / ".cursor" / "skills" / "chat-report"
    assert (runtime_dir / "run.py").is_file()
    assert (runtime_dir / "chat_report" / "cli.py").is_file()
    assert (bin_dir / "chat-report.cmd").is_file()
    assert (bin_dir / "chat-report").is_file()
    assert (user_skill_dir / "SKILL.md").is_file()
    assert not (user_skill_dir / "scripts").exists()
    assert "<skill_dir>/scripts/run.py" not in (user_skill_dir / "SKILL.md").read_text(encoding="utf-8")

    cmd_text = (bin_dir / "chat-report.cmd").read_text(encoding="utf-8")
    posix_text = (bin_dir / "chat-report").read_text(encoding="utf-8")
    assert "%%VAR%%" in cmd_text
    assert ":run_python\n" in cmd_text
    assert "exit /b %ERRORLEVEL%\n" in cmd_text
    assert "'\"'\"'" in posix_text

    # Re-installing without --force fails across runtime, shims, or skill
    assert main(["--install", "--runtime-dir", str(runtime_dir), "--bin-dir", str(bin_dir)]) == 1
    capsys.readouterr()

    # Put bin_dir on PATH and re-run with --force: no PATH warning should fire
    current_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{current_path}")
    assert main(["--install", "--runtime-dir", str(runtime_dir), "--bin-dir", str(bin_dir), "--force"]) == 0
    assert "bin-dir-not-on-path" not in capsys.readouterr().err

    # Execute the native platform shim
    native_shim = bin_dir / ("chat-report.cmd" if sys.platform == "win32" else "chat-report")
    env_with_py = dict(os.environ)
    env_with_py["CHAT_REPORT_PYTHON"] = sys.executable

    ver_res = subprocess.run(
        [str(native_shim), "--version"],
        cwd=tmp_path,
        env=env_with_py,
        capture_output=True,
        text=True,
        check=True,
    )
    assert ver_res.stdout.strip() == "chat-report 2.0.2 (schema 2.0)"

    state = create_synthetic_state_db(tmp_path / "state.vscdb")
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db")
    out_dir = tmp_path / "shim_out"
    run_res = subprocess.run(
        [
            str(native_shim),
            "--latest",
            "--state-db",
            str(state),
            "--tracking-db",
            str(tracking),
            "--out",
            str(out_dir),
            "--agent-digest",
        ],
        cwd=tmp_path,
        env=env_with_py,
        capture_output=True,
        text=True,
        check=True,
    )
    assert run_res.returncode == 0
    assert (out_dir / f"{DEFAULT_SESSION_ID}.json").is_file()

    # Verify non-zero exit code propagation through native shim
    fail_res = subprocess.run(
        [str(native_shim), "--latest", "--state-db", str(tmp_path / "missing.vscdb")],
        cwd=tmp_path,
        env=env_with_py,
        capture_output=True,
        text=True,
    )
    assert fail_res.returncode == 1

    # Verify interpreter fallback when CHAT_REPORT_PYTHON points to an incompatible/failing interpreter
    bad_py_dir = tmp_path / "bad_py"
    bad_py_dir.mkdir()
    if sys.platform == "win32":
        bad_py = bad_py_dir / "bad_python.exe"
        bad_py.write_bytes(b"not-a-valid-executable")
        fallback_env = dict(os.environ)
        fallback_env["CHAT_REPORT_PYTHON"] = str(bad_py)
        fallback_env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"
        fb_res = subprocess.run(
            [str(native_shim), "--version"],
            cwd=tmp_path,
            env=fallback_env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert fb_res.stdout.strip() == "chat-report 2.0.2 (schema 2.0)"

        # And when no compatible Python 3.10+ interpreter exists on PATH
        empty_bin = tmp_path / "empty_bin"
        empty_bin.mkdir()
        no_py_env = dict(os.environ)
        no_py_env["CHAT_REPORT_PYTHON"] = str(bad_py)
        no_py_env["PATH"] = str(empty_bin)
        no_py_res = subprocess.run(
            [str(native_shim), "--version"],
            cwd=tmp_path,
            env=no_py_env,
            capture_output=True,
            text=True,
        )
        assert no_py_res.returncode == 1
        assert "Python 3.10+ interpreter was not found" in no_py_res.stderr
    else:
        bad_py = bad_py_dir / "python3"
        bad_py.write_text("#!/usr/bin/env sh\nexit 1\n", encoding="utf-8")
        bad_py.chmod(0o755)
        fallback_env = dict(os.environ)
        fallback_env["CHAT_REPORT_PYTHON"] = str(bad_py)
        fb_res = subprocess.run(
            [str(native_shim), "--version"],
            cwd=tmp_path,
            env=fallback_env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert fb_res.stdout.strip() == "chat-report 2.0.2 (schema 2.0)"

    # --no-skill skips creating skill dir
    no_skill_rt = tmp_path / "rt_no_skill"
    no_skill_bin = tmp_path / "bin_no_skill"
    custom_skill = tmp_path / "should_not_exist_skill"
    assert main([
        "--install",
        "--runtime-dir", str(no_skill_rt),
        "--bin-dir", str(no_skill_bin),
        "--skill-dir", str(custom_skill),
        "--no-skill",
    ]) == 0
    assert not custom_skill.exists()

    # --install-skill into workspace scope and converting an existing full bundle to lightweight skill
    ws_dir = tmp_path / "workspace_proj"
    ws_dir.mkdir()
    ws_skill = ws_dir / ".cursor" / "skills" / "chat-report"
    assert main(["--bundle-into", str(ws_skill)]) == 0
    assert (ws_skill / "scripts" / "run.py").is_file()
    assert main(["--install-skill", "--workspace", str(ws_dir)]) == 1
    assert main(["--install-skill", "--workspace", str(ws_dir), "--force"]) == 0
    assert (ws_skill / "SKILL.md").is_file()
    assert not (ws_skill / "scripts").exists()
    assert "chat-report --latest --agent-digest" in (ws_skill / "SKILL.md").read_text(encoding="utf-8")


def test_install_cli_multi_target_rollback(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "rb_runtime"
    bin_dir = tmp_path / "rb_bin"
    skill_dir = tmp_path / "rb_skill"

    assert main([
        "--install",
        "--runtime-dir", str(runtime_dir),
        "--bin-dir", str(bin_dir),
        "--skill-dir", str(skill_dir),
    ]) == 0
    (runtime_dir / "sentinel.txt").write_text("old_rt", encoding="utf-8")
    (bin_dir / "chat-report.cmd").write_text("old_cmd", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text("old_skill", encoding="utf-8")

    real_replace = os.replace
    replace_count = 0

    def fail_on_skill_swap(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        nonlocal replace_count
        replace_count += 1
        # 1-2: runtime backup+stage, 3-6: cmd+posix shim backup+stage, 7: skill backup, 8: skill stage->dest
        if replace_count == 8:
            raise OSError("simulated skill swap failure")
        real_replace(src, dst)

    with pytest.raises(OSError, match="simulated skill swap failure"):
        installer.install_cli(
            runtime_dir=runtime_dir,
            bin_dir=bin_dir,
            skill_dir=skill_dir,
            force=True,
            replace_fn=fail_on_skill_swap,
        )

    assert (runtime_dir / "sentinel.txt").read_text(encoding="utf-8") == "old_rt"
    assert (bin_dir / "chat-report.cmd").read_text(encoding="utf-8") == "old_cmd"
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "old_skill"
    assert not list(tmp_path.glob("*.stage.*"))
    assert not list(tmp_path.glob("*.bak.*"))
    assert not list(bin_dir.glob("*.stage.*"))
    assert not list(bin_dir.glob("*.bak.*"))

    # Also test failure on the very first runtime swap (replace_count == 2) when force=True:
    # must preserve the existing runtime_dir without deleting it!
    replace_count = 0

    def fail_on_runtime_swap(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        nonlocal replace_count
        replace_count += 1
        if replace_count == 2:
            raise OSError("simulated runtime swap failure")
        real_replace(src, dst)

    with pytest.raises(OSError, match="simulated runtime swap failure"):
        installer.install_cli(
            runtime_dir=runtime_dir,
            bin_dir=bin_dir,
            skill_dir=skill_dir,
            force=True,
            replace_fn=fail_on_runtime_swap,
        )
    assert (runtime_dir / "sentinel.txt").read_text(encoding="utf-8") == "old_rt"
    assert (bin_dir / "chat-report.cmd").read_text(encoding="utf-8") == "old_cmd"
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "old_skill"
    assert not list(tmp_path.glob("*.stage.*"))
    assert not list(tmp_path.glob("*.bak.*"))

    # Verify source skill overlap is rejected across --install and --install-skill using a simulated checkout
    sim_checkout = tmp_path / "sim_overlap_repo"
    sim_pkg = sim_checkout / "chat_report"
    sim_skill = sim_checkout / "skills" / "chat-report"
    sim_pkg.mkdir(parents=True)
    sim_skill.mkdir(parents=True)
    (sim_skill / "SKILL.md").write_text("canonical", encoding="utf-8")
    with pytest.raises(ValueError, match="overlaps the source skill or package"):
        installer.handle_install_argv(
            ["--install-skill", "--skill-dir", str(sim_skill), "--force"],
            source_package=sim_pkg,
        )
    with pytest.raises(ValueError, match="overlaps the source skill or package"):
        installer.handle_install_argv(
            [
                "--install",
                "--runtime-dir", str(tmp_path / "rt_overlap"),
                "--bin-dir", str(tmp_path / "bin_overlap"),
                "--skill-dir", str(sim_skill),
                "--force",
            ],
            source_package=sim_pkg,
        )
    assert (sim_skill / "SKILL.md").read_text(encoding="utf-8") == "canonical"


def test_locked_directory_permission_error_in_place_fallback(tmp_path: Path) -> None:
    """When an IDE/OS watcher holds a directory handle (WinError 5 on top-level rename), swap contents in-place."""
    skill_dir = tmp_path / "watched_skill"
    runtime_dir = tmp_path / "watched_rt"
    bin_dir = tmp_path / "watched_bin"

    # First populate skill_dir with a full bundle (containing scripts/run.py)
    assert main(["--bundle-into", str(skill_dir)]) == 0
    assert (skill_dir / "scripts" / "run.py").is_file()

    real_replace = os.replace

    def deny_top_level_skill_dir_rename(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if Path(src).resolve() == skill_dir.resolve():
            raise PermissionError(5, "Access is denied", str(src), 5, str(dst))
        real_replace(src, dst)

    installer.install_cli(
        runtime_dir=runtime_dir,
        bin_dir=bin_dir,
        skill_dir=skill_dir,
        force=True,
        replace_fn=deny_top_level_skill_dir_rename,
    )
    assert (skill_dir / "SKILL.md").is_file()
    assert not (skill_dir / "scripts").exists()
    assert "chat-report --latest --agent-digest" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.stage.*"))
    assert not list(tmp_path.glob("*.bak.*"))

    # Also test in-place rollback if moving staged content into locked skill_dir fails mid-way
    (skill_dir / "SKILL.md").write_text("preserved_lightweight", encoding="utf-8")

    def deny_top_level_and_fail_stage_move(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        src_p = Path(src).resolve()
        dst_p = Path(dst).resolve()
        if src_p == skill_dir.resolve():
            raise PermissionError(5, "Access is denied", str(src), 5, str(dst))
        if dst_p == (skill_dir / "SKILL.md").resolve() and ".stage." in str(src_p):
            raise OSError("simulated stage child move failure")
        real_replace(src, dst)

    with pytest.raises(OSError, match="simulated stage child move failure"):
        installer.install_skill(
            skill_dir=skill_dir,
            force=True,
            replace_fn=deny_top_level_and_fail_stage_move,
        )
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "preserved_lightweight"
    assert not list(tmp_path.glob("*.stage.*"))
    assert not list(tmp_path.glob("*.bak.*"))

    # Also test partial evacuation rollback: if dest has multiple children (alpha.txt, beta.txt)
    # and the second dest->backup child move fails, both alpha.txt and beta.txt must survive!
    (skill_dir / "alpha.txt").write_text("alpha_orig", encoding="utf-8")
    (skill_dir / "beta.txt").write_text("beta_orig", encoding="utf-8")
    evac_moves = 0

    def deny_top_level_and_fail_second_evac(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        nonlocal evac_moves
        src_p = Path(src).resolve()
        dst_p = Path(dst).resolve()
        if src_p == skill_dir.resolve():
            raise PermissionError(5, "Access is denied", str(src), 5, str(dst))
        if src_p.parent == skill_dir.resolve() and ".bak." in str(dst_p):
            evac_moves += 1
            if evac_moves == 2:
                raise OSError("simulated partial backup failure")
        real_replace(src, dst)

    with pytest.raises(OSError, match="simulated partial backup failure"):
        installer.install_skill(
            skill_dir=skill_dir,
            force=True,
            replace_fn=deny_top_level_and_fail_second_evac,
        )
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "preserved_lightweight"
    assert (skill_dir / "alpha.txt").read_text(encoding="utf-8") == "alpha_orig"
    assert (skill_dir / "beta.txt").read_text(encoding="utf-8") == "beta_orig"
    assert not list(tmp_path.glob("*.stage.*"))
    assert not list(tmp_path.glob("*.bak.*"))


def test_wheel_build_and_isolated_console_script_execution(tmp_path: Path) -> None:
    repository = Path(__file__).parents[2]
    wheel_dir = tmp_path / "wheelhouse"
    wheel_dir.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-build-isolation",
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
            str(repository),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    wheels = list(wheel_dir.glob("chat_report-*.whl"))
    assert len(wheels) == 1

    install_target = tmp_path / "isolated_pkg"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            "--target",
            str(install_target),
            str(wheels[0]),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    bin_candidates = [
        install_target / "bin" / "chat-report.exe",
        install_target / "bin" / "chat-report",
        install_target / "Scripts" / "chat-report.exe",
        install_target / "Scripts" / "chat-report",
    ]
    console_exe = next((item for item in bin_candidates if item.is_file()), None)
    assert console_exe is not None
    assert (install_target / "chat_report" / "assets" / "SKILL.md").is_file()

    isolated_env = dict(os.environ)
    isolated_env["PYTHONPATH"] = str(install_target)

    ver_res = subprocess.run(
        [str(console_exe), "--version"],
        cwd=tmp_path,
        env=isolated_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert ver_res.stdout.strip() == "chat-report 2.0.2 (schema 2.0)"

    # Verify --install-skill and --bundle-into work from the isolated wheel without repo skills/
    wheel_skill = tmp_path / "wheel_skill"
    subprocess.run(
        [str(console_exe), "--install-skill", "--skill-dir", str(wheel_skill)],
        cwd=tmp_path,
        env=isolated_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert (wheel_skill / "SKILL.md").is_file()
    assert "chat-report --latest --agent-digest" in (wheel_skill / "SKILL.md").read_text(encoding="utf-8")

    wheel_bundle = tmp_path / "wheel_bundle"
    subprocess.run(
        [str(console_exe), "--bundle-into", str(wheel_bundle)],
        cwd=tmp_path,
        env=isolated_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert (wheel_bundle / "SKILL.md").is_file()
    assert (wheel_bundle / "scripts" / "run.py").is_file()
    assert (wheel_bundle / "scripts" / "chat_report" / "cli.py").is_file()


