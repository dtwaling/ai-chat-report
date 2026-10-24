"""Command-line interface for AI Chat Report v2."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chat_report.adapters
from chat_report.core.adapter import ExtractOptions, ResolvedTarget, SessionNotFoundError, StoreAccessError, get_adapter
from chat_report.core.classifier import classify_session
from chat_report.core.compare import build_aggregate_report, build_diff_report, build_single_session_report
from chat_report.core.render.digest import render_agent_digest
from chat_report.core.render.json_v2 import (
    assert_v2_report_conforms,
    render_aggregate_json,
    render_diff_json,
    render_single_session_json,
)
from chat_report.core.render.markdown import render_aggregate_md, render_diff_md, render_single_session_md


_BOOLEAN_FLAGS = frozenset({
    "--latest",
    "--request-id",
    "--chat-id",
    "--agent-digest",
    "--no-subagents",
    "--list-include-subagents",
    "--include-orphans",
    "--verbose",
})
_TRUE_LITERALS = frozenset({"true", "1", "yes"})
_FALSE_LITERALS = frozenset({"false", "0", "no"})
_BOOL_LITERALS = _TRUE_LITERALS | _FALSE_LITERALS


_ILLEGAL_PATH_CHARS = frozenset('"<>|?*\x00')


def _clean_path(raw: str) -> Path:
    cleaned = raw.strip().strip("\"'").strip()
    if not cleaned:
        raise argparse.ArgumentTypeError("path cannot be empty")
    if '"' in cleaned or "\x00" in cleaned:
        raise argparse.ArgumentTypeError(f"path contains illegal characters: {cleaned}")
    check_target = cleaned[4:] if cleaned.startswith("\\\\?\\") else cleaned
    if sys.platform == "win32" and any(char in _ILLEGAL_PATH_CHARS for char in check_target):
        raise argparse.ArgumentTypeError(f"path contains illegal characters: {cleaned}")
    return Path(cleaned)


def _normalize_boolean_argv(argv: list[str]) -> list[str]:
    """Normalize explicit boolean values on CLI boolean flags before argparse runs."""
    normalized: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            normalized.extend(argv[index:])
            break
        if token in _BOOLEAN_FLAGS:
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
            if flag in _BOOLEAN_FLAGS:
                lowered = value.strip().lower()
                if lowered in _TRUE_LITERALS:
                    normalized.append(flag)
                    index += 1
                    continue
                if lowered in _FALSE_LITERALS:
                    index += 1
                    continue
        normalized.append(token)
        index += 1
    return normalized


def _resolve_bundler_script() -> Path:
    here = Path(__file__).resolve().parent
    bundled_script = here.parent / "run.py"
    if bundled_script.is_file():
        return bundled_script
    checkout_script = here.parent / "skills" / "chat-report" / "scripts" / "run.py"
    if checkout_script.is_file():
        return checkout_script
    raise ValueError("Bundler script skills/chat-report/scripts/run.py was not found.")


def strip_deprecated_v1_argv(argv: list[str]) -> tuple[list[str], bool]:
    """Remove deprecated v1 shape/tool flags before argparse sees them."""
    cleaned: list[str] = []
    stripped = False
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument in {"--shape", "--tools"}:
            stripped = True
            if index + 1 < len(argv) and not argv[index + 1].startswith("-"):
                index += 2
            else:
                index += 1
            continue
        if argument.startswith("--shape=") or argument.startswith("--tools="):
            stripped = True
            index += 1
            continue
        cleaned.append(argument)
        index += 1
    return cleaned, stripped


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chat-report")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("session_id", nargs="?")
    mode.add_argument("--latest", action="store_true")
    mode.add_argument("--list-recent", nargs="?", const=10, type=int, metavar="N")
    mode.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"))
    mode.add_argument("--aggregate", nargs="+", metavar="SESSION_ID")
    mode.add_argument("--validate", type=_clean_path, metavar="JSON_PATH")
    id_kind = parser.add_mutually_exclusive_group()
    id_kind.add_argument("--request-id", action="store_true")
    id_kind.add_argument("--chat-id", action="store_true")
    parser.add_argument("--harness", default="cursor")
    parser.add_argument("--out", type=_clean_path, default=Path(".cursor/local/chat-reports"))
    parser.add_argument("--format", choices=("md", "json", "both"), default="both")
    parser.add_argument("--agent-digest", action="store_true")
    parser.add_argument("--no-subagents", action="store_true")
    parser.add_argument("--list-include-subagents", action="store_true")
    parser.add_argument("--include-orphans", action="store_true")
    parser.add_argument("--workspace", type=_clean_path)
    parser.add_argument("--state-db", type=_clean_path)
    parser.add_argument("--tracking-db", type=_clean_path)
    parser.add_argument("--denial-marker", action="append", default=[])
    parser.add_argument("--verbose", action="store_true")
    return parser


def _reserve_aggregate_paths(
    out_dir: Path,
    session_ids: list[str],
    output_format: str,
    contents: list[str],
) -> list[Path]:
    cohort = hashlib.sha256(",".join(session_ids).encode("utf-8")).hexdigest()[:8]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    stem = f"aggregate-{timestamp}-{cohort}"
    suffixes = [".json", ".md"] if output_format == "both" else [f".{output_format}"]
    sequence = 1
    while True:
        candidate = stem if sequence == 1 else f"{stem}-{sequence}"
        paths = [out_dir / f"{candidate}{suffix}" for suffix in suffixes]
        descriptors: list[int] = []
        try:
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptors.append(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            for descriptor in descriptors:
                os.close(descriptor)
            for path in paths[:len(descriptors)]:
                path.unlink(missing_ok=True)
            sequence += 1
            continue
        except OSError:
            for descriptor in descriptors:
                os.close(descriptor)
            for path in paths[:len(descriptors)]:
                path.unlink(missing_ok=True)
            raise
        try:
            for index, (descriptor, content) in enumerate(zip(descriptors, contents)):
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    descriptors[index] = -1
                    handle.write(content)
        except BaseException:
            for descriptor in descriptors:
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        return paths


def _write_report(paths: list[Path], json_text: str, markdown: str) -> None:
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json_text if path.suffix == ".json" else markdown, encoding="utf-8")


def _extract(adapter: Any, raw_id: str, options: ExtractOptions) -> Any:
    target = adapter.resolve_id(raw_id, options)
    return classify_session(adapter.extract_session(target.session_id, options, resolved_from=target), options.workspace)


def _options(args: argparse.Namespace, workspace: Path) -> ExtractOptions:
    return ExtractOptions(
        state_db=args.state_db,
        tracking_db=args.tracking_db,
        workspace=workspace,
        include_subagents=not args.no_subagents,
        list_include_subagents=args.list_include_subagents,
        include_orphans=args.include_orphans,
        force_id_kind="request-id" if args.request_id else "chat-id" if args.chat_id else None,
        extra_denial_markers=args.denial_marker,
        verbose=args.verbose,
    )


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Path:
    if args.session_id is None and not any((args.latest, args.list_recent is not None, args.diff, args.aggregate, args.validate)):
        parser.error("provide a session ID or one mode flag")
    if args.aggregate is not None and len(args.aggregate) < 2:
        parser.error("--aggregate requires at least two session IDs")
    if args.list_include_subagents and args.list_recent is None:
        parser.error("--list-include-subagents requires --list-recent")
    if args.agent_digest and (args.list_recent is not None or args.diff or args.aggregate or args.validate):
        parser.error("--agent-digest supports only a session ID or --latest")
    if args.latest and (args.request_id or args.chat_id):
        parser.error("--latest cannot be combined with --request-id or --chat-id")
    if args.workspace is not None and (not args.workspace.exists() or not args.workspace.is_dir()):
        raise ValueError(f"--workspace must be an existing directory: {args.workspace}")
    return args.workspace if args.workspace is not None else Path.cwd()


def main(argv: list[str] | None = None) -> int:
    """Execute CLI mode and return a process exit code."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if any(arg == "--bundle-into" or arg.startswith("--bundle-into=") for arg in raw_argv):
            script_path = _resolve_bundler_script()
            spec = importlib.util.spec_from_file_location("chat_report_skill_bundler", script_path)
            if spec is None or spec.loader is None:
                raise ValueError(f"Unable to load bundler script: {script_path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            destination, force = module._bundle_args(raw_argv)
            if destination is not None:
                module.bundle_into(destination, force)
                return 0
        argv, stripped = strip_deprecated_v1_argv(raw_argv)
        if stripped:
            print("[chat-report] WARNING: deprecated-v1-flag: --shape/--tools are deprecated in v2.0 and ignored.", file=sys.stderr)
        argv = _normalize_boolean_argv(argv)
        parser = _parser()
        args = parser.parse_args(argv)
        workspace = _validate_args(parser, args)
        if args.validate:
            payload = json.loads(args.validate.read_text(encoding="utf-8"))
            assert_v2_report_conforms(payload)
            print(f"OK: {args.validate} conforms to v2.0 ({payload['report_kind']})")
            return 0
        adapter = get_adapter(args.harness)
        options = _options(args, workspace)
        if args.list_recent is not None:
            for item in adapter.list_recent(args.list_recent, options):
                print(f"{item.session_id}\t{item.last_updated_at_ms or item.created_at_ms or 0}\t{item.title or ''}\t{item.model_name or ''}\tsubagent={item.is_subagent}")
            return 0
        if args.latest:
            entries = adapter.list_recent(1, options)
            if not entries:
                raise SessionNotFoundError("No Cursor sessions were found.")
            target = ResolvedTarget(session_id=entries[0].session_id, input_id="--latest", source="latest")
            session = classify_session(adapter.extract_session(target.session_id, options, resolved_from=target), workspace)
            report = build_single_session_report(session, workspace=workspace)
            markdown = render_single_session_md(report)
            stem = report["session"]["session_id"]
            json_text = render_single_session_json(report)
        elif args.diff:
            before, after = (_extract(adapter, value, options) for value in args.diff)
            report = build_diff_report(before, after, workspace=workspace)
            markdown = render_diff_md(report)
            stem = f"diff-{before.session_id[:12]}-vs-{after.session_id[:12]}"
            json_text = render_diff_json(report)
        elif args.aggregate:
            sessions = [_extract(adapter, value, options) for value in args.aggregate]
            report = build_aggregate_report(sessions, workspace=workspace)
            markdown = render_aggregate_md(report)
            json_text = render_aggregate_json(report)
            suffixes = [".json", ".md"] if args.format == "both" else [f".{args.format}"]
            contents = [json_text if suffix == ".json" else markdown for suffix in suffixes]
            paths = _reserve_aggregate_paths(args.out, [item.session_id_normalized for item in sessions], args.format, contents)
            for path in paths:
                print(path)
            return 0
        else:
            report = build_single_session_report(_extract(adapter, args.session_id, options), workspace=workspace)
            markdown = render_single_session_md(report)
            stem = report["session"]["session_id"]
            json_text = render_single_session_json(report)
        if args.agent_digest and args.format in ("md", "json"):
            print("[chat-report] WARNING: digest-forced-both-formats: --agent-digest requires both .json and .md files; overriding --format.", file=sys.stderr)
        output_format = "both" if args.agent_digest else args.format
        paths = [args.out / f"{stem}{suffix}" for suffix in ((".json", ".md") if output_format == "both" else (f".{output_format}",))]
        _write_report(paths, json_text, markdown)
        if args.agent_digest:
            json_path = next(path for path in paths if path.suffix == ".json")
            markdown_path = next(path for path in paths if path.suffix == ".md")
            print(render_agent_digest(report, json_path, markdown_path), end="")
        else:
            for path in paths:
                print(path)
        return 0
    except (OSError, ValueError, KeyError, SessionNotFoundError, StoreAccessError, AssertionError, json.JSONDecodeError) as exc:
        print(f"[chat-report] ERROR: {exc}", file=sys.stderr)
        return 1
