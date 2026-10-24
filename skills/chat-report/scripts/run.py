"""Launch a checkout or self-contained chat-report skill bundle."""

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
