"""Launch a checkout or self-contained chat-report skill bundle."""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import uuid
from pathlib import Path


_SCRIPT_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SCRIPT_DIR.parent
_BUNDLED_PACKAGE = _SCRIPT_DIR / "chat_report"
_CHECKOUT_ROOT = _SKILL_ROOT.parent.parent
_CHECKOUT_PACKAGE = _CHECKOUT_ROOT / "chat_report"


def _package_dir() -> Path:
    return _BUNDLED_PACKAGE if _BUNDLED_PACKAGE.is_dir() else _CHECKOUT_PACKAGE


def _copy_bundle(stage: Path, package: Path) -> None:
    shutil.copy2(_SKILL_ROOT / "SKILL.md", stage / "SKILL.md")
    scripts = stage / "scripts"
    scripts.mkdir()
    shutil.copy2(Path(__file__), scripts / "run.py")
    shutil.copytree(package, scripts / "chat_report", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def bundle_into(destination: str, force: bool = False) -> None:
    """Atomically install this skill and its Python package into destination."""
    dest = Path(destination).resolve()
    package = _package_dir().resolve()
    skill = _SKILL_ROOT.resolve()
    if any(dest == source or dest.is_relative_to(source) or source.is_relative_to(dest) for source in (skill, package)):
        raise ValueError("Bundle destination overlaps the source skill or package.")
    if dest.exists() and any(dest.iterdir()) and not force:
        raise ValueError("Bundle destination is non-empty; pass --force to replace it.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    stage = dest.parent / f".{dest.name}.stage.{token}"
    backup = dest.parent / f".{dest.name}.bak.{token}"
    try:
        stage.mkdir()
        _copy_bundle(stage, package)
        if dest.exists():
            os.replace(dest, backup)
        os.replace(stage, dest)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        if backup.exists():
            try:
                if dest.exists():
                    shutil.rmtree(dest)
                os.replace(backup, dest)
            except OSError as rollback_exc:
                print(
                    f"[chat-report] WARNING: bundle rollback failed to restore {dest} from {backup}: {rollback_exc}",
                    file=sys.stderr,
                )
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
            if backup.exists():
                print(
                    f"[chat-report] WARNING: bundle backup cleanup left temporary directory {backup}",
                    file=sys.stderr,
                )


def _bundle_args(argv: list[str]) -> tuple[str | None, bool]:
    if "--bundle-into" not in argv:
        return None, False
    index = argv.index("--bundle-into")
    if index + 1 >= len(argv):
        raise ValueError("--bundle-into requires a destination.")
    remaining = argv[:index] + argv[index + 2:]
    force = "--force" in remaining
    remaining = [arg for arg in remaining if arg != "--force"]
    if remaining:
        raise ValueError("--bundle-into cannot be combined with report arguments.")
    return argv[index + 1], force


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        destination, force = _bundle_args(arguments)
        if destination is not None:
            bundle_into(destination, force)
            return 0
        package = _package_dir()
        if not package.is_dir():
            raise ValueError("chat_report package was not found beside this skill.")
        sys.path.insert(0, str(package.parent))
        return importlib.import_module("chat_report.cli").main(arguments)
    except (OSError, ValueError) as exc:
        print(f"[chat-report] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
