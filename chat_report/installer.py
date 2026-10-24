"""Atomic installer and bundler for global CLI, lightweight skill, and portable skill bundles."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import stat
import sys
import time
import uuid
from pathlib import Path
from typing import Callable


_PACKAGE_DIR = Path(__file__).resolve().parent
_PACKAGED_SKILL_MD = _PACKAGE_DIR / "assets" / "SKILL.md"
_TRUE_LITERALS = frozenset({"true", "1", "yes"})
_FALSE_LITERALS = frozenset({"false", "0", "no"})
_BOOL_LITERALS = _TRUE_LITERALS | _FALSE_LITERALS
_ILLEGAL_DEST_CHARS = frozenset('"<>|?*\r\n\x00')
_HARNESS_RE = re.compile(r"^[a-z0-9_-]+$")
_TRAILING_FORCE_RE = re.compile(
    r"""["']\s+(?:--force|-f)(?:(?:=|\s+)(?P<val>true|false|1|0|yes|no))?\s*$""",
    re.IGNORECASE,
)
_INSTALL_MODE_FLAGS = frozenset({"--bundle-into", "--install", "--install-skill"})
_INSTALL_OPTION_FLAGS = frozenset({
    "--bundle-into",
    "--install",
    "--install-skill",
    "--bin-dir",
    "--runtime-dir",
    "--skill-dir",
    "--no-skill",
    "--workspace",
    "--harness",
    "--force",
    "-f",
})

_RUN_PY_SOURCE = '''"""Launch a checkout or self-contained chat-report skill bundle."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import shutil
import sys
import uuid


_SCRIPT_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SCRIPT_DIR.parent
_BUNDLED_PACKAGE = _SCRIPT_DIR / "chat_report"
_CHECKOUT_ROOT = _SKILL_ROOT.parent.parent
_CHECKOUT_PACKAGE = _CHECKOUT_ROOT / "chat_report"


def _package_dir() -> Path:
    return _BUNDLED_PACKAGE if _BUNDLED_PACKAGE.is_dir() else _CHECKOUT_PACKAGE


def _ensure_package_importable() -> Path:
    package = _package_dir()
    if not package.is_dir():
        raise ValueError("chat_report package was not found beside this skill.")
    parent = str(package.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    return package


def _load_installer():
    _ensure_package_importable()
    return importlib.import_module("chat_report.installer")


def _bundle_args(argv: list[str]) -> tuple[str | None, bool]:
    return _load_installer()._bundle_args(argv)


def bundle_into(destination: str, force: bool = False) -> None:
    """Atomically install this skill and its Python package into destination."""
    _load_installer().bundle_into(
        destination,
        force=force,
        source_package=_package_dir(),
        source_skill=_SKILL_ROOT if (_SKILL_ROOT / "SKILL.md").is_file() else None,
        replace_fn=os.replace,
        rmtree_fn=shutil.rmtree,
        uuid_fn=lambda: uuid.uuid4().hex,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        installer = _load_installer()
        if installer.has_install_flags(arguments):
            installer.handle_install_argv(
                arguments,
                source_package=_package_dir(),
                source_skill=_SKILL_ROOT if (_SKILL_ROOT / "SKILL.md").is_file() else None,
            )
            return 0
        return importlib.import_module("chat_report.cli").main(arguments)
    except (OSError, ValueError) as exc:
        print(f"[chat-report] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _read_canonical_skill_md(source_skill: Path | None = None) -> str:
    if source_skill is not None and (source_skill / "SKILL.md").is_file():
        return (source_skill / "SKILL.md").read_text(encoding="utf-8")
    if _PACKAGED_SKILL_MD.is_file():
        return _PACKAGED_SKILL_MD.read_text(encoding="utf-8")
    repo_skill = _PACKAGE_DIR.parent / "skills" / "chat-report" / "SKILL.md"
    if repo_skill.is_file():
        return repo_skill.read_text(encoding="utf-8")
    raise ValueError("Canonical SKILL.md asset was not found.")


def render_lightweight_skill_md(source_skill: Path | None = None) -> str:
    """Render SKILL.md with bundled runner paths replaced by the global CLI."""
    raw = _read_canonical_skill_md(source_skill)
    rendered = raw.replace('python "<skill_dir>/scripts/run.py"', "chat-report")
    if "<skill_dir>/scripts/run.py" in rendered:
        raise ValueError("Lightweight SKILL.md rendering left unreplaced runner references.")
    return rendered


def render_cmd_shim(runtime_dir: Path) -> str:
    """Render the Windows CMD launcher shim with safe % escaping and exit propagation."""
    run_py = str(runtime_dir.resolve() / "run.py")
    if any(ch in _ILLEGAL_DEST_CHARS for ch in (run_py[4:] if run_py.startswith("\\\\?\\") else run_py)):
        raise ValueError(f"Runtime path contains illegal characters: {runtime_dir}")
    cmd_escaped = run_py.replace("%", "%%")
    probe = '-c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"'
    return (
        "@echo off\n"
        "if defined CHAT_REPORT_PYTHON (\n"
        f'    "%CHAT_REPORT_PYTHON%" {probe} >nul 2>nul\n'
        "    if not errorlevel 1 goto :run_env\n"
        ")\n"
        f"python {probe} >nul 2>nul\n"
        "if not errorlevel 1 goto :run_python\n"
        f"py -3 {probe} >nul 2>nul\n"
        "if not errorlevel 1 goto :run_py3\n"
        f"python3 {probe} >nul 2>nul\n"
        "if not errorlevel 1 goto :run_python3\n"
        "echo [chat-report] ERROR: Python 3.10+ interpreter was not found on PATH. 1>&2\n"
        "exit /b 1\n"
        ":run_env\n"
        f'"%CHAT_REPORT_PYTHON%" "{cmd_escaped}" %*\n'
        "exit /b %ERRORLEVEL%\n"
        ":run_python\n"
        f'python "{cmd_escaped}" %*\n'
        "exit /b %ERRORLEVEL%\n"
        ":run_py3\n"
        f'py -3 "{cmd_escaped}" %*\n'
        "exit /b %ERRORLEVEL%\n"
        ":run_python3\n"
        f'python3 "{cmd_escaped}" %*\n'
        "exit /b %ERRORLEVEL%\n"
    )


def render_posix_shim(runtime_dir: Path) -> str:
    """Render the POSIX sh launcher shim with shlex token quoting and Python 3.10+ probe."""
    run_py_resolved = runtime_dir.resolve() / "run.py"
    raw_str = str(run_py_resolved)
    if any(ch in _ILLEGAL_DEST_CHARS for ch in (raw_str[4:] if raw_str.startswith("\\\\?\\") else raw_str)):
        raise ValueError(f"Runtime path contains illegal characters: {runtime_dir}")
    sh_quoted_run_py = shlex.quote(run_py_resolved.as_posix())
    probe = "-c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'"
    return (
        "#!/usr/bin/env sh\n"
        f'if [ -n "${{CHAT_REPORT_PYTHON:-}}" ] && "$CHAT_REPORT_PYTHON" {probe} >/dev/null 2>&1; then\n'
        f'    exec "$CHAT_REPORT_PYTHON" {sh_quoted_run_py} "$@"\n'
        "fi\n"
        "for candidate in python3 python; do\n"
        f'    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" {probe} >/dev/null 2>&1; then\n'
        f'        exec "$candidate" {sh_quoted_run_py} "$@"\n'
        "    fi\n"
        "done\n"
        'echo "[chat-report] ERROR: Python 3.10+ interpreter was not found on PATH." >&2\n'
        "exit 1\n"
    )


def default_runtime_dir() -> Path:
    env_override = os.environ.get("CHAT_REPORT_RUNTIME_DIR", "").strip()
    if env_override:
        cleaned, _ = _clean_destination(env_override, "--runtime-dir")
        return Path(cleaned)
    home = Path.home()
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_app_data) if local_app_data else (home / "AppData" / "Local")
        return base / "chat-report"
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg_data) if xdg_data else (home / ".local" / "share")
    return base / "chat-report"


def default_bin_dir() -> Path:
    env_override = os.environ.get("CHAT_REPORT_BIN_DIR", "").strip()
    if env_override:
        cleaned, _ = _clean_destination(env_override, "--bin-dir")
        return Path(cleaned)
    return Path.home() / ".local" / "bin"


def validate_harness_name(harness: str) -> str:
    cleaned = harness.strip().lower()
    if not _HARNESS_RE.match(cleaned):
        raise ValueError(f"Invalid harness name: {harness}")
    return cleaned


def resolve_skill_dir(
    harness: str = "cursor",
    workspace: Path | str | None = None,
    skill_dir: Path | str | None = None,
) -> Path:
    validated_harness = validate_harness_name(harness)
    if skill_dir is not None:
        cleaned, _ = _clean_destination(str(skill_dir), "--skill-dir")
        return Path(cleaned)
    if workspace is not None:
        cleaned_ws, _ = _clean_destination(str(workspace), "--workspace")
        ws_path = Path(cleaned_ws).resolve()
        if not ws_path.exists() or not ws_path.is_dir():
            raise ValueError(f"--workspace must be an existing directory: {workspace}")
        return ws_path / f".{validated_harness}" / "skills" / "chat-report"
    return Path.home() / f".{validated_harness}" / "skills" / "chat-report"


def _clean_destination(destination: str, flag_name: str = "--bundle-into") -> tuple[str, bool | None]:
    raw = destination
    inferred_force: bool | None = None
    match = _TRAILING_FORCE_RE.search(raw)
    if match is not None:
        raw = raw[:match.start()]
        val = match.group("val")
        inferred_force = (val.lower() in _TRUE_LITERALS) if val is not None else True
    cleaned = raw.strip().strip("\"'").strip()
    if not cleaned:
        raise ValueError(f"{flag_name} requires a destination.")
    if any(tok in cleaned for tok in ("--force", "--bundle-into", "--install", "--install-skill")):
        raise ValueError(f"Destination contains embedded flag tokens: {cleaned}")
    check_target = cleaned[4:] if cleaned.startswith("\\\\?\\") else cleaned
    if any(char in _ILLEGAL_DEST_CHARS for char in check_target):
        raise ValueError(f"Destination contains illegal path characters: {cleaned}")
    return cleaned, inferred_force


def _parse_force_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE_LITERALS:
        return True
    if normalized in _FALSE_LITERALS:
        return False
    raise ValueError(f"Invalid --force value: {value}")


def _paths_overlap(first: Path, second: Path) -> bool:
    a = first.resolve()
    b = second.resolve()
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _resolve_sources(source_package: Path | None = None, source_skill: Path | None = None) -> tuple[Path, list[Path]]:
    package = (source_package or _PACKAGE_DIR).resolve()
    sources = [package]
    if source_skill is not None and source_skill.exists():
        sources.append(source_skill.resolve())
    else:
        repo_skill = package.parent / "skills" / "chat-report"
        if repo_skill.exists():
            sources.append(repo_skill.resolve())
    return package, sources


def _check_source_overlap(targets: list[Path], sources: list[Path]) -> None:
    for target in targets:
        for source in sources:
            if _paths_overlap(target, source):
                raise ValueError("Destination overlaps the source skill or package.")
    for index, first in enumerate(targets):
        for second in targets[index + 1:]:
            if _paths_overlap(first, second):
                raise ValueError(f"Installation targets overlap each other: {first} vs {second}")


def _is_dir_on_path(target_dir: Path) -> bool:
    raw_path = os.environ.get("PATH", "")
    if not raw_path:
        return False
    resolved_target = str(target_dir.resolve()).rstrip("\\/")
    if sys.platform == "win32":
        resolved_target = resolved_target.lower()
    for entry in raw_path.split(os.pathsep):
        cleaned = entry.strip().strip('"')
        if not cleaned:
            continue
        try:
            candidate = str(Path(cleaned).resolve()).rstrip("\\/")
        except OSError:
            candidate = cleaned.rstrip("\\/")
        if sys.platform == "win32":
            candidate = candidate.lower()
        if candidate == resolved_target:
            return True
    return False


def _copy_bundle(stage: Path, package: Path, source_skill: Path | None = None) -> None:
    (stage / "SKILL.md").write_text(_read_canonical_skill_md(source_skill), encoding="utf-8")
    scripts = stage / "scripts"
    scripts.mkdir()
    (scripts / "run.py").write_text(_RUN_PY_SOURCE, encoding="utf-8")
    shutil.copytree(package, scripts / "chat_report", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _copy_runtime(stage: Path, package: Path) -> None:
    (stage / "run.py").write_text(_RUN_PY_SOURCE, encoding="utf-8")
    shutil.copytree(package, stage / "chat_report", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _copy_lightweight_skill(stage: Path, source_skill: Path | None = None) -> None:
    (stage / "SKILL.md").write_text(render_lightweight_skill_md(source_skill), encoding="utf-8")


def _remove_entry(
    target: Path,
    rmtree_fn: Callable[..., None] = shutil.rmtree,
    *,
    ignore_errors: bool = False,
) -> None:
    if not target.exists() and not target.is_symlink():
        return
    if target.is_dir() and not target.is_symlink():
        for root_dir, dirs, files in os.walk(target, topdown=False):
            for file_name in files:
                file_path = Path(root_dir) / file_name
                try:
                    os.chmod(file_path, stat.S_IWRITE)
                except OSError:
                    pass
                if rmtree_fn is shutil.rmtree:
                    try:
                        file_path.unlink(missing_ok=True)
                    except PermissionError:
                        if not ignore_errors:
                            raise
            for dir_name in dirs:
                dir_path = Path(root_dir) / dir_name
                try:
                    os.chmod(dir_path, stat.S_IWRITE)
                except OSError:
                    pass
        try:
            os.chmod(target, stat.S_IWRITE)
        except OSError:
            pass
        for attempt in range(10):
            if not target.exists():
                return
            try:
                rmtree_fn(target)
                return
            except PermissionError:
                if attempt == 9:
                    if ignore_errors:
                        rmtree_fn(target, ignore_errors=True)
                        return
                    raise
                time.sleep(0.02)
    else:
        try:
            os.chmod(target, stat.S_IWRITE)
        except OSError:
            pass
        try:
            target.unlink(missing_ok=True)
        except OSError:
            if not ignore_errors:
                raise


def _move_entry(
    src: Path,
    dst: Path,
    *,
    replace_fn: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
    rmtree_fn: Callable[..., None] = shutil.rmtree,
) -> None:
    try:
        replace_fn(src, dst)
    except PermissionError:
        if dst.exists() or dst.is_symlink():
            _remove_entry(dst, rmtree_fn=rmtree_fn)
        try:
            if src.is_dir() and not src.is_symlink():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        except BaseException:
            if dst.exists() or dst.is_symlink():
                _remove_entry(dst, rmtree_fn=rmtree_fn, ignore_errors=True)
            raise
        _remove_entry(src, rmtree_fn=rmtree_fn)


def _restore_dir_contents_in_place(
    dest: Path,
    backup: Path,
    *,
    dest_evacuated: bool = True,
    replace_fn: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
    rmtree_fn: Callable[..., None] = shutil.rmtree,
) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if dest_evacuated:
        for child in list(dest.iterdir()):
            _remove_entry(child, rmtree_fn=rmtree_fn)
    if backup.exists():
        for child in list(backup.iterdir()):
            target_child = dest / child.name
            if target_child.exists() or target_child.is_symlink():
                _remove_entry(target_child, rmtree_fn=rmtree_fn)
            _move_entry(child, target_child, replace_fn=replace_fn, rmtree_fn=rmtree_fn)
        _remove_entry(backup, rmtree_fn=rmtree_fn, ignore_errors=True)


def _atomic_replace_dir(
    dest: Path,
    populate_fn: Callable[[Path], None],
    *,
    replace_fn: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
    rmtree_fn: Callable[..., None] = shutil.rmtree,
    token: str,
) -> Path | None:
    """Stage a directory via populate_fn and swap it into dest, returning backup path if any."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    stage = dest.parent / f".{dest.name}.stage.{token}"
    backup = dest.parent / f".{dest.name}.bak.{token}"
    had_existing = False
    in_place_mode = False
    dest_evacuated = False
    created_dest = False
    try:
        stage.mkdir()
        populate_fn(stage)
        if dest.exists():
            try:
                replace_fn(dest, backup)
                had_existing = True
                dest_evacuated = True
            except PermissionError:
                in_place_mode = True
                had_existing = True
                backup.mkdir(parents=True, exist_ok=True)
                for child in list(dest.iterdir()):
                    _move_entry(child, backup / child.name, replace_fn=replace_fn, rmtree_fn=rmtree_fn)
                dest_evacuated = True
                for child in list(stage.iterdir()):
                    _move_entry(child, dest / child.name, replace_fn=replace_fn, rmtree_fn=rmtree_fn)
                _remove_entry(stage, rmtree_fn=rmtree_fn, ignore_errors=True)
                return backup
        try:
            replace_fn(stage, dest)
        except PermissionError:
            in_place_mode = True
            if not dest.exists():
                dest.mkdir(parents=True, exist_ok=True)
                created_dest = not had_existing
            for child in list(stage.iterdir()):
                _move_entry(child, dest / child.name, replace_fn=replace_fn, rmtree_fn=rmtree_fn)
            _remove_entry(stage, rmtree_fn=rmtree_fn, ignore_errors=True)
        return backup if had_existing else None
    except BaseException:
        if stage.exists():
            _remove_entry(stage, rmtree_fn=rmtree_fn, ignore_errors=True)
        if created_dest and dest.exists():
            _remove_entry(dest, rmtree_fn=rmtree_fn, ignore_errors=True)
        if had_existing and backup.exists():
            try:
                if in_place_mode:
                    _restore_dir_contents_in_place(
                        dest,
                        backup,
                        dest_evacuated=dest_evacuated,
                        replace_fn=replace_fn,
                        rmtree_fn=rmtree_fn,
                    )
                else:
                    if dest.exists():
                        rmtree_fn(dest)
                    replace_fn(backup, dest)
            except PermissionError:
                try:
                    _restore_dir_contents_in_place(
                        dest,
                        backup,
                        dest_evacuated=dest_evacuated,
                        replace_fn=replace_fn,
                        rmtree_fn=rmtree_fn,
                    )
                except OSError as rollback_exc:
                    print(
                        f"[chat-report] WARNING: bundle rollback failed to restore {dest} from {backup}: {rollback_exc}",
                        file=sys.stderr,
                    )
            except OSError as rollback_exc:
                print(
                    f"[chat-report] WARNING: bundle rollback failed to restore {dest} from {backup}: {rollback_exc}",
                    file=sys.stderr,
                )
        raise


def _cleanup_dir_backup(backup: Path | None, rmtree_fn: Callable[..., None] = shutil.rmtree) -> None:
    if backup is not None and backup.exists():
        _remove_entry(backup, rmtree_fn=rmtree_fn, ignore_errors=True)
        if backup.exists():
            print(
                f"[chat-report] WARNING: bundle backup cleanup left temporary directory {backup}",
                file=sys.stderr,
            )


def _rollback_dir(
    dest: Path,
    backup: Path | None,
    *,
    replace_fn: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
    rmtree_fn: Callable[..., None] = shutil.rmtree,
) -> None:
    try:
        try:
            if dest.exists():
                rmtree_fn(dest)
            if backup is not None and backup.exists():
                replace_fn(backup, dest)
        except PermissionError:
            if backup is not None and backup.exists():
                _restore_dir_contents_in_place(dest, backup, replace_fn=replace_fn, rmtree_fn=rmtree_fn)
            elif dest.exists():
                for child in list(dest.iterdir()):
                    _remove_entry(child, rmtree_fn=rmtree_fn)
    except OSError as rollback_exc:
        print(
            f"[chat-report] WARNING: bundle rollback failed to restore {dest} from {backup}: {rollback_exc}",
            file=sys.stderr,
        )


def bundle_into(
    destination: str,
    force: bool = False,
    *,
    source_package: Path | None = None,
    source_skill: Path | None = None,
    replace_fn: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
    rmtree_fn: Callable[..., None] = shutil.rmtree,
    uuid_fn: Callable[[], str] | None = None,
) -> Path:
    """Atomically install this skill and its Python package into destination."""
    cleaned_dest, inferred_force = _clean_destination(destination, "--bundle-into")
    if inferred_force is not None and not force:
        force = inferred_force
    dest = Path(cleaned_dest).resolve()
    package, sources = _resolve_sources(source_package, source_skill)
    _check_source_overlap([dest], sources)
    if dest.exists() and any(dest.iterdir()) and not force:
        raise ValueError("Bundle destination is non-empty; pass --force to replace it.")
    token = uuid_fn() if uuid_fn is not None else uuid.uuid4().hex
    backup = _atomic_replace_dir(
        dest,
        lambda stage: _copy_bundle(stage, package, source_skill),
        replace_fn=replace_fn,
        rmtree_fn=rmtree_fn,
        token=token,
    )
    _cleanup_dir_backup(backup, rmtree_fn=rmtree_fn)
    return dest


def install_skill(
    skill_dir: Path | str | None = None,
    *,
    harness: str = "cursor",
    workspace: Path | str | None = None,
    force: bool = False,
    source_package: Path | None = None,
    source_skill: Path | None = None,
    check_cli_on_path: bool = True,
    replace_fn: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
    rmtree_fn: Callable[..., None] = shutil.rmtree,
) -> Path:
    """Atomically install the lightweight SKILL.md (referencing global chat-report)."""
    target_skill = resolve_skill_dir(harness=harness, workspace=workspace, skill_dir=skill_dir).resolve()
    _, sources = _resolve_sources(source_package, source_skill)
    _check_source_overlap([target_skill], sources)
    if target_skill.exists() and any(target_skill.iterdir()) and not force:
        raise ValueError(f"Skill destination is non-empty ({target_skill}); pass --force to replace it.")
    token = uuid.uuid4().hex
    backup = _atomic_replace_dir(
        target_skill,
        lambda stage: _copy_lightweight_skill(stage, source_skill),
        replace_fn=replace_fn,
        rmtree_fn=rmtree_fn,
        token=token,
    )
    _cleanup_dir_backup(backup, rmtree_fn=rmtree_fn)
    if check_cli_on_path and shutil.which("chat-report") is None:
        print(
            "[chat-report] WARNING: cli-not-on-path: 'chat-report' was not found on PATH; "
            "run 'python -m chat_report --install' or ensure the CLI directory is on PATH.",
            file=sys.stderr,
        )
    return target_skill


def install_cli(
    *,
    runtime_dir: Path | str | None = None,
    bin_dir: Path | str | None = None,
    skill_dir: Path | str | None = None,
    harness: str = "cursor",
    workspace: Path | str | None = None,
    include_skill: bool = True,
    force: bool = False,
    source_package: Path | None = None,
    source_skill: Path | None = None,
    replace_fn: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
    rmtree_fn: Callable[..., None] = shutil.rmtree,
) -> tuple[Path, Path, Path | None]:
    """Atomically install the global runtime, CLI shims, and optional lightweight skill."""
    if runtime_dir is not None:
        cleaned_rt, inferred_rt = _clean_destination(str(runtime_dir), "--runtime-dir")
        if inferred_rt is not None and not force:
            force = inferred_rt
        resolved_runtime = Path(cleaned_rt).resolve()
    else:
        resolved_runtime = default_runtime_dir().resolve()

    if bin_dir is not None:
        cleaned_bin, inferred_bin = _clean_destination(str(bin_dir), "--bin-dir")
        if inferred_bin is not None and not force:
            force = inferred_bin
        resolved_bin = Path(cleaned_bin).resolve()
    else:
        resolved_bin = default_bin_dir().resolve()

    resolved_skill: Path | None = None
    if include_skill:
        resolved_skill = resolve_skill_dir(harness=harness, workspace=workspace, skill_dir=skill_dir).resolve()

    package, sources = _resolve_sources(source_package, source_skill)
    targets = [resolved_runtime, resolved_bin]
    if resolved_skill is not None:
        targets.append(resolved_skill)
    _check_source_overlap(targets, sources)

    cmd_shim = resolved_bin / "chat-report.cmd"
    posix_shim = resolved_bin / "chat-report"
    if resolved_runtime.exists() and any(resolved_runtime.iterdir()) and not force:
        raise ValueError(f"Runtime destination is non-empty ({resolved_runtime}); pass --force to replace it.")
    if (cmd_shim.exists() or posix_shim.exists()) and not force:
        raise ValueError(f"CLI shim already exists in {resolved_bin}; pass --force to replace it.")
    if resolved_skill is not None and resolved_skill.exists() and any(resolved_skill.iterdir()) and not force:
        raise ValueError(f"Skill destination is non-empty ({resolved_skill}); pass --force to replace it.")

    cmd_text = render_cmd_shim(resolved_runtime)
    posix_text = render_posix_shim(resolved_runtime)

    token = uuid.uuid4().hex
    runtime_backup: Path | None = None
    runtime_swapped = False
    skill_backup: Path | None = None
    shim_stages: list[Path] = []
    shim_backups: list[tuple[Path, Path | None]] = []

    try:
        runtime_backup = _atomic_replace_dir(
            resolved_runtime,
            lambda stage: _copy_runtime(stage, package),
            replace_fn=replace_fn,
            rmtree_fn=rmtree_fn,
            token=token,
        )
        runtime_swapped = True

        resolved_bin.mkdir(parents=True, exist_ok=True)
        for shim_path, content, is_exec in (
            (cmd_shim, cmd_text, False),
            (posix_shim, posix_text, True),
        ):
            stage_file = resolved_bin / f".{shim_path.name}.stage.{token}"
            bak_file = resolved_bin / f".{shim_path.name}.bak.{token}"
            shim_stages.append(stage_file)
            stage_file.write_text(content, encoding="utf-8", newline="\n")
            if is_exec:
                stage_file.chmod(0o755)
            had_shim = False
            if shim_path.exists():
                replace_fn(shim_path, bak_file)
                had_shim = True
            shim_backups.append((shim_path, bak_file if had_shim else None))
            replace_fn(stage_file, shim_path)

        if resolved_skill is not None:
            skill_backup = _atomic_replace_dir(
                resolved_skill,
                lambda stage: _copy_lightweight_skill(stage, source_skill),
                replace_fn=replace_fn,
                rmtree_fn=rmtree_fn,
                token=token,
            )
    except BaseException:
        for stage_file in shim_stages:
            stage_file.unlink(missing_ok=True)
        for shim_path, bak_file in reversed(shim_backups):
            try:
                if shim_path.exists():
                    shim_path.unlink()
                if bak_file is not None and bak_file.exists():
                    replace_fn(bak_file, shim_path)
            except OSError as rollback_exc:
                print(
                    f"[chat-report] WARNING: shim rollback failed to restore {shim_path}: {rollback_exc}",
                    file=sys.stderr,
                )
        if runtime_swapped:
            _rollback_dir(resolved_runtime, runtime_backup, replace_fn=replace_fn, rmtree_fn=rmtree_fn)
        raise
    else:
        _cleanup_dir_backup(runtime_backup, rmtree_fn=rmtree_fn)
        _cleanup_dir_backup(skill_backup, rmtree_fn=rmtree_fn)
        for _, bak_file in shim_backups:
            if bak_file is not None and bak_file.exists():
                bak_file.unlink(missing_ok=True)

    if not _is_dir_on_path(resolved_bin):
        print(
            f"[chat-report] WARNING: bin-dir-not-on-path: {resolved_bin} is not on PATH; "
            "add it to PATH so 'chat-report' can be invoked directly.",
            file=sys.stderr,
        )
    return resolved_runtime, resolved_bin, resolved_skill


def has_install_flags(argv: list[str]) -> bool:
    for arg in argv:
        if arg in _INSTALL_MODE_FLAGS:
            return True
        if any(arg.startswith(f"{flag}=") for flag in _INSTALL_MODE_FLAGS):
            return True
    return False


def _bundle_args(argv: list[str]) -> tuple[str | None, bool]:
    if not any(arg == "--bundle-into" or arg.startswith("--bundle-into=") for arg in argv):
        return None, False
    normalized: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--bundle-into":
            if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                raise ValueError("--bundle-into requires a destination.")
            normalized.extend(("--bundle-into", argv[index + 1]))
            index += 2
            continue
        if token.startswith("--bundle-into="):
            value = token.split("=", 1)[1]
            if not value.strip():
                raise ValueError("--bundle-into requires a destination.")
            normalized.append(token)
            index += 1
            continue
        if token in {"--force", "-f"}:
            if index + 1 < len(argv) and argv[index + 1].strip().lower() in _BOOL_LITERALS:
                normalized.append(f"--force={argv[index + 1].strip().lower()}")
                index += 2
            else:
                normalized.append("--force=true")
                index += 1
            continue
        if token.startswith("--force=") or token.startswith("-f="):
            value = token.split("=", 1)[1]
            _parse_force_bool(value)
            normalized.append(f"--force={value.strip().lower()}")
            index += 1
            continue
        normalized.append(token)
        index += 1

    parser = argparse.ArgumentParser(prog="chat-report", add_help=False)
    parser.add_argument("--bundle-into", dest="destinations", action="append", default=[])
    parser.add_argument("--force", "-f", dest="force_values", action="append", default=[])
    parser.add_argument("extras", nargs="*")
    known, unknown = parser.parse_known_args(normalized)
    if unknown or len(known.destinations) != 1 or len(known.extras) > 1:
        raise ValueError("--bundle-into cannot be combined with report arguments.")
    destination, inferred_force = _clean_destination(known.destinations[0], "--bundle-into")
    force_candidates: list[bool] = []
    if inferred_force is not None:
        force_candidates.append(inferred_force)
    for value in known.force_values:
        force_candidates.append(_parse_force_bool(value))
    if known.extras:
        extra = known.extras[0].strip().lower()
        if extra == "force" or extra in _TRUE_LITERALS:
            force_candidates.append(True)
        elif extra in _FALSE_LITERALS:
            force_candidates.append(False)
        else:
            raise ValueError("--bundle-into cannot be combined with report arguments.")
    if len(set(force_candidates)) > 1:
        raise ValueError("Conflicting --force values were supplied.")
    force = force_candidates[-1] if force_candidates else False
    return destination, force


def _normalize_install_argv(argv: list[str]) -> list[str]:
    normalized: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in {"--force", "-f"}:
            if index + 1 < len(argv) and argv[index + 1].strip().lower() in _BOOL_LITERALS:
                normalized.append(f"--force={argv[index + 1].strip().lower()}")
                index += 2
            else:
                normalized.append("--force=true")
                index += 1
            continue
        if token.startswith("--force=") or token.startswith("-f="):
            value = token.split("=", 1)[1]
            _parse_force_bool(value)
            normalized.append(f"--force={value.strip().lower()}")
            index += 1
            continue
        if token in {"--install", "--install-skill", "--no-skill"}:
            if index + 1 < len(argv) and argv[index + 1].strip().lower() in _BOOL_LITERALS:
                if argv[index + 1].strip().lower() in _TRUE_LITERALS:
                    normalized.append(token)
                index += 2
                continue
            normalized.append(token)
            index += 1
            continue
        if "=" in token:
            flag, value = token.split("=", 1)
            if flag in {"--install", "--install-skill", "--no-skill"}:
                if _parse_force_bool(value):
                    normalized.append(flag)
                index += 1
                continue
        normalized.append(token)
        index += 1
    return normalized


def handle_install_argv(
    argv: list[str],
    *,
    source_package: Path | None = None,
    source_skill: Path | None = None,
) -> None:
    """Parse and execute --bundle-into, --install, or --install-skill."""
    has_bundle = any(arg == "--bundle-into" or arg.startswith("--bundle-into=") for arg in argv)
    has_install = any(arg == "--install" or arg.startswith("--install=") for arg in argv)
    has_install_skill = any(arg == "--install-skill" or arg.startswith("--install-skill=") for arg in argv)
    if sum((has_bundle, has_install, has_install_skill)) > 1:
        raise ValueError("--bundle-into, --install, and --install-skill are mutually exclusive.")
    if has_bundle:
        destination, force = _bundle_args(argv)
        if destination is not None:
            bundle_into(destination, force=force, source_package=source_package, source_skill=source_skill)
        return

    normalized = _normalize_install_argv(argv)
    parser = argparse.ArgumentParser(prog="chat-report", add_help=False)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--install-skill", action="store_true")
    parser.add_argument("--bin-dir", default=None)
    parser.add_argument("--runtime-dir", default=None)
    parser.add_argument("--skill-dir", default=None)
    parser.add_argument("--no-skill", action="store_true")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--harness", default="cursor")
    parser.add_argument("--force", "-f", dest="force_values", action="append", default=[])
    parser.add_argument("extras", nargs="*")
    known, unknown = parser.parse_known_args(normalized)
    mode_name = "--install" if has_install else "--install-skill"
    if unknown or len(known.extras) > 1:
        raise ValueError(f"{mode_name} cannot be combined with report arguments.")
    if not known.install and not known.install_skill:
        raise ValueError(f"{mode_name} was disabled; provide a valid mode flag.")

    force_candidates: list[bool] = [_parse_force_bool(v) for v in known.force_values]
    if known.extras:
        extra = known.extras[0].strip().lower()
        if extra == "force" or extra in _TRUE_LITERALS:
            force_candidates.append(True)
        elif extra in _FALSE_LITERALS:
            force_candidates.append(False)
        else:
            raise ValueError(f"{mode_name} cannot be combined with report arguments.")
    if len(set(force_candidates)) > 1:
        raise ValueError("Conflicting --force values were supplied.")
    force = force_candidates[-1] if force_candidates else False

    if known.install_skill:
        if known.bin_dir is not None or known.runtime_dir is not None or known.no_skill:
            raise ValueError("--install-skill does not accept --bin-dir, --runtime-dir, or --no-skill.")
        skill_path = install_skill(
            skill_dir=known.skill_dir,
            harness=known.harness,
            workspace=known.workspace,
            force=force,
            source_package=source_package,
            source_skill=source_skill,
        )
        print(skill_path)
        return

    runtime_path, bin_path, skill_path = install_cli(
        runtime_dir=known.runtime_dir,
        bin_dir=known.bin_dir,
        skill_dir=known.skill_dir,
        harness=known.harness,
        workspace=known.workspace,
        include_skill=not known.no_skill,
        force=force,
        source_package=source_package,
        source_skill=source_skill,
    )
    print(runtime_path)
    print(bin_path)
    if skill_path is not None:
        print(skill_path)
