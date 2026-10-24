"""Launch a checkout or self-contained chat-report skill bundle."""

from __future__ import annotations

import argparse
import importlib
import os
import re
import shutil
import sys
import uuid
from pathlib import Path


_SCRIPT_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SCRIPT_DIR.parent
_BUNDLED_PACKAGE = _SCRIPT_DIR / "chat_report"
_CHECKOUT_ROOT = _SKILL_ROOT.parent.parent
_CHECKOUT_PACKAGE = _CHECKOUT_ROOT / "chat_report"
_TRUE_LITERALS = frozenset({"true", "1", "yes"})
_FALSE_LITERALS = frozenset({"false", "0", "no"})
_BOOL_LITERALS = _TRUE_LITERALS | _FALSE_LITERALS
_ILLEGAL_DEST_CHARS = frozenset('"<>|?*\x00')
_TRAILING_FORCE_RE = re.compile(
    r"""["']\s+(?:--force|-f)(?:(?:=|\s+)(?P<val>true|false|1|0|yes|no))?\s*$""",
    re.IGNORECASE,
)


def _package_dir() -> Path:
    return _BUNDLED_PACKAGE if _BUNDLED_PACKAGE.is_dir() else _CHECKOUT_PACKAGE


def _copy_bundle(stage: Path, package: Path) -> None:
    shutil.copy2(_SKILL_ROOT / "SKILL.md", stage / "SKILL.md")
    scripts = stage / "scripts"
    scripts.mkdir()
    shutil.copy2(Path(__file__), scripts / "run.py")
    shutil.copytree(package, scripts / "chat_report", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _clean_destination(destination: str) -> tuple[str, bool | None]:
    raw = destination
    inferred_force: bool | None = None
    match = _TRAILING_FORCE_RE.search(raw)
    if match is not None:
        raw = raw[:match.start()]
        val = match.group("val")
        inferred_force = (val.lower() in _TRUE_LITERALS) if val is not None else True
    cleaned = raw.strip().strip("\"'").strip()
    if not cleaned:
        raise ValueError("--bundle-into requires a destination.")
    if "--force" in cleaned or "--bundle-into" in cleaned:
        raise ValueError(f"Bundle destination contains embedded flag tokens: {cleaned}")
    check_target = cleaned[4:] if cleaned.startswith("\\\\?\\") else cleaned
    if any(char in _ILLEGAL_DEST_CHARS for char in check_target):
        raise ValueError(f"Bundle destination contains illegal path characters: {cleaned}")
    return cleaned, inferred_force


def _parse_force_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE_LITERALS:
        return True
    if normalized in _FALSE_LITERALS:
        return False
    raise ValueError(f"Invalid --force value: {value}")


def bundle_into(destination: str, force: bool = False) -> None:
    """Atomically install this skill and its Python package into destination."""
    cleaned_dest, inferred_force = _clean_destination(destination)
    if inferred_force is not None and not force:
        force = inferred_force
    dest = Path(cleaned_dest).resolve()
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
    destination, inferred_force = _clean_destination(known.destinations[0])
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
