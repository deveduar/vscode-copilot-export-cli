"""Command line interface for the  vscode-copilot-export-cli"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import __version__
from .db import DatabaseError
from .discovery import DiscoveryError, SourceSet, discover
from .filters import CONTENT_TYPES, FilterOptions, filter_sessions, parse_turns
from .models import Session
from .normalize import assemble
from .progress import Progress
from .selection import (
    DEFAULT_ORDER,
    ORDER_CHOICES,
    SessionSelection,
    chronological,
    parse_session_ids,
    parse_since,
    select_sessions,
)
from .writers import (
    DEFAULT_MAX_SESSION_MD_BYTES,
    WriteOptions,
    create_run_directory,
    find_run_directory,
    plan_outputs,
    write_exports,
    write_metadata,
)

EXIT_OK = 0
EXIT_SOURCE_ERROR = 1
EXIT_NO_DATA = 2

CONTENT_DESCRIPTIONS = {
    "prompts": "User requests.",
    "answers": "Rendered assistant responses.",
    "reasoning": "Stored thinking content when the editor persisted it.",
    "tool-calls": "Serialized tool invocations and their available input.",
    "tool-results": "Explicit tool results stored separately by the editor.",
    "edits": "Serialized text edits and diffs.",
    "references": "Standalone references and code-block references.",
    "tool-events": "Tool lifecycle and status events.",
}


def _configure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="copilot-export",
        description=(
            "Export GitHub Copilot chats from VS Code state.vscdb, agent session "
            "databases, and native .jsonl chat sessions."
        ),
        epilog=(
            "Examples:\n"
            "  copilot-export\n"
            "  copilot-export --format markdown\n"
            "  copilot-export --format jsonl --jsonl-compact\n"
            "  copilot-export --format json --format jsonl\n"
            "  copilot-export --turns 1-10 --content prompts --content answers\n"
            "  copilot-export --last-turns 3 --format markdown\n"
            "  copilot-export --split-long-sessions --max-session-md-bytes 800000\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source_pos", nargs="?", metavar="SOURCE", help=argparse.SUPPRESS)
    parser.add_argument(
        "-s",
        "--source",
        help=(
            "Path to a VS Code chat database, a .jsonl chat log, or a directory "
            "to scan."
        ),
    )
    parser.add_argument(
        "--db",
        help="Explicit path to a state.vscdb database (adds adjacent chat sessions).",
    )
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument(
        "--check",
        action="store_true",
        help="Report discovered sources and session count without writing output.",
    )
    operation.add_argument(
        "--list-sessions",
        action="store_true",
        help="List matching sessions without writing output; combine with --json.",
    )
    operation.add_argument(
        "--capabilities",
        action="store_true",
        help="Describe supported content, formats, and presets; does not read sources.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="./export",
        help="Root directory for timestamped export bundles (default: ./export).",
    )
    parser.add_argument(
        "--label",
        help="Optional label for this run's bundle directory (default: selected content types).",
    )
    parser.add_argument(
        "-f",
        "--format",
        action="append",
        choices=("markdown", "json", "jsonl"),
        dest="formats",
        help="Output format; repeat to write multiple formats. Defaults to markdown and json.",
    )
    parser.add_argument(
        "--jsonl-compact",
        action="store_true",
        help=(
            "Write JSONL with one session header and omit repeated session metadata "
            "from part/usage records (requires JSONL output)."
        ),
    )
    parser.add_argument(
        "--split-long-sessions",
        action="store_true",
        help="Split large per-session Markdown files without cutting messages.",
    )
    split_limit = parser.add_mutually_exclusive_group()
    split_limit.add_argument(
        "--max-session-md-bytes",
        type=_positive_integer,
        default=DEFAULT_MAX_SESSION_MD_BYTES,
        metavar="N",
        help=(
            "Maximum Markdown part size before splitting "
            f"(default: {DEFAULT_MAX_SESSION_MD_BYTES})."
        ),
    )
    split_limit.add_argument(
        "--max-session-md-mb",
        type=_positive_integer,
        default=None,
        metavar="N",
        help="Maximum Markdown part size in megabytes (converted to bytes internally).",
    )
    content_selection = parser.add_mutually_exclusive_group()
    content_selection.add_argument(
        "--content",
        action="append",
        choices=CONTENT_TYPES,
        help=(
            "Content to export; repeat to combine types (default: all). "
            "Tool results are selected separately from assistant answers."
        ),
    )
    content_selection.add_argument(
        "--conversation",
        action="store_true",
        help=(
            "Export prompts, assistant answers, and stored reasoning while keeping "
            "inline references in place and omitting standalone tool results and other "
            "tool content."
        ),
    )
    parser.add_argument(
        "--no-omission-markers",
        action="store_false",
        dest="omission_markers",
        default=True,
        help=(
            "Do not insert content-filter omission notices into exported messages "
            "(this does not change content selection; details remain in metadata and "
            "turn-gap notices are unaffected)."
        ),
    )
    parser.add_argument(
        "--max-reasoning-chars",
        type=_positive_integer,
        metavar="N",
        help="Truncate each reasoning block to at most N characters.",
    )
    parser.add_argument(
        "--include-raw-reasoning",
        action="store_true",
        help=(
            "Include serialized reasoning blocks with no readable text "
            "(excluded by default)."
        ),
    )
    parser.add_argument(
        "--max-tool-output-chars",
        type=_positive_integer,
        default=None,
        metavar="N",
        help=(
            "Optionally retain at most N source characters per tool result, plus a "
            "truncation marker (disabled by default)."
        ),
    )
    parser.add_argument(
        "--truncate-direction",
        choices=("beginning", "end"),
        default="end",
        help="Keep the beginning or end of truncated content (default: end).",
    )
    turn_selection = parser.add_mutually_exclusive_group()
    turn_selection.add_argument(
        "--turns",
        action="append",
        default=[],
        metavar="RANGE",
        help="One-based turns to export, e.g. 1-3,7,10-. Repeatable.",
    )
    turn_selection.add_argument(
        "--last-turns",
        type=_positive_integer,
        metavar="N",
        help="Export the last N turns of each session; mutually exclusive with --turns.",
    )
    session_selection = parser.add_argument_group("session selection")
    session_selection.add_argument(
        "--sessions",
        metavar="ID[,ID]",
        help="Only include the given comma-separated session IDs.",
    )
    session_selection.add_argument(
        "--last",
        type=_positive_integer,
        metavar="N",
        help="Only include the latest N sessions, ordered by --order.",
    )
    session_selection.add_argument(
        "--since",
        metavar="DATE",
        help="Only include sessions updated on or after an ISO date or timestamp.",
    )
    session_selection.add_argument(
        "--filter",
        metavar="TEXT",
        help="Only include sessions whose title, workspace, ID, or source file contains TEXT.",
    )
    session_selection.add_argument(
        "--order",
        choices=ORDER_CHOICES,
        default=DEFAULT_ORDER,
        help=f"Session ordering for --last and --list-sessions (default: {DEFAULT_ORDER}).",
    )
    progress = parser.add_mutually_exclusive_group()
    progress.add_argument(
        "--progress",
        action="store_true",
        help="Force progress on stderr when it is not a terminal (automatic in terminals).",
    )
    progress.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable the progress indicator.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Do not write normal status text to stdout; only errors go to stderr.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help=(
            "Emit a machine-readable object for --check, --list-sessions, or "
            "--capabilities."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be exported without writing any file.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _format_list(sources) -> List[str]:
    paths: List[str] = []
    for source in sources.dbs:
        paths.append(str(source.path))
    for source in sources.jsonls:
        paths.append(str(source.path))
    for source in sources.agent_sessions:
        paths.append(str(source.path))
    return paths


def _source_summary(
    sources: SourceSet,
    source_path: Optional[str],
    db_path: Optional[str],
) -> Dict[str, object]:
    explicit = db_path or source_path
    stores = [
        {
            "kind": "state.vscdb",
            "path": str(source.path),
            "workspace": source.workspace.workspace_id,
        }
        for source in sources.dbs
    ]
    stores.extend(
        {
            "kind": "jsonl",
            "path": str(source.path),
            "workspace": source.workspace.workspace_id,
        }
        for source in sources.jsonls
    )
    stores.extend(
        {
            "kind": "agentSessionData",
            "path": str(source.path),
            "workspace": source.workspace.workspace_id,
        }
        for source in sources.agent_sessions
    )
    return {
        "resolved": str(Path(explicit).expanduser()) if explicit else "auto",
        "exists": True,
        "origin": "explicit" if explicit else "auto",
        "stores": stores,
    }


def _source_error_summary(
    source_path: Optional[str],
    db_path: Optional[str],
) -> Dict[str, object]:
    explicit = db_path or source_path
    return {
        "resolved": str(Path(explicit).expanduser()) if explicit else "auto",
        "exists": False,
        "origin": "explicit" if explicit else "auto",
        "stores": [],
    }


def _emit_json(payload: Dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _readonly_error(
    args: argparse.Namespace,
    source_info: Dict[str, object],
    error: str,
) -> int:
    if args.json_output:
        _emit_json(
            {
                "schema_version": 1,
                "tool": "copilot_export",
                "version": __version__,
                "ok": False,
                "status": "error",
                "source": source_info,
                "session_count": None,
                "sessions_found": None,
                "no_data_reason": None,
                "error": error,
                "writes": False,
                "sessions": [],
            }
        )
    elif not args.quiet:
        print(f"error: {error}", file=sys.stderr)
    return EXIT_SOURCE_ERROR


def _workspace_label(session: Session, workspaces: Dict[str, object]) -> str:
    workspace = workspaces.get(session.workspace_id)
    path = getattr(workspace, "path", "") if workspace is not None else ""
    return path or session.workspace_id


def _session_turn_count(session: Session) -> int:
    return max(
        max((message.turn for message in session.messages), default=0),
        sum(message.role == "user" for message in session.messages),
    )


def _check_payload(
    sources: SourceSet,
    selected: List[Session],
    selection: SessionSelection,
    source_path: Optional[str],
    db_path: Optional[str],
) -> Dict[str, object]:
    no_data_reason = None if selected else selection.no_data_reason()
    status = "no_data" if no_data_reason else "ready"
    return {
        "schema_version": 1,
        "tool": "copilot_export",
        "version": __version__,
        "ok": status == "ready",
        "status": status,
        "source": _source_summary(sources, source_path, db_path),
        "session_count": len(selected),
        "no_data_reason": no_data_reason,
        "error": None,
        "writes": False,
    }


def _list_payload(
    sources: SourceSet,
    selected: List[Session],
    selection: SessionSelection,
    workspaces: Dict[str, object],
    source_path: Optional[str],
    db_path: Optional[str],
) -> Dict[str, object]:
    no_data_reason = None if selected else selection.no_data_reason()
    status = "no_data" if no_data_reason else "ready"
    records = []
    for session in selected:
        records.append(
            {
                "id": session.session_id,
                "title": session.title,
                "created": session.created_at,
                "updated": session.updated_at,
                "revision": int(session.updated_ts * 1_000_000),
                "workspace": _workspace_label(session, workspaces),
                "workspace_id": session.workspace_id,
                "agent": session.agent or None,
                "source": session.source or None,
                "source_file": session.source_file or None,
                "message_count": len(session.messages),
                "turn_count": _session_turn_count(session),
            }
        )
    return {
        "schema_version": 1,
        "tool": "copilot_export",
        "version": __version__,
        "ok": status == "ready",
        "status": status,
        "source": _source_summary(sources, source_path, db_path),
        "sessions_found": len(records),
        "no_data_reason": no_data_reason,
        "error": None,
        "writes": False,
        "sessions": records,
    }


def _capabilities_payload() -> Dict[str, object]:
    return {
        "schema_version": 1,
        "tool": "copilot_export",
        "version": __version__,
        "writes": False,
        "formats": ["markdown", "json", "jsonl"],
        "content": [
            {"name": name, "description": CONTENT_DESCRIPTIONS[name]}
            for name in CONTENT_TYPES
        ],
        "presets": {
            "conversation": {
                "native": True,
                "content": ["prompts", "answers", "reasoning"],
                "description": (
                    "Dialogue-focused output that keeps inline references and "
                    "omits standalone tool content."
                ),
            },
            "full": {
                "native": False,
                "content": list(CONTENT_TYPES),
                "formats": ["markdown", "json", "jsonl"],
                "description": (
                    "Most inclusive supported bundle; launchers compose it from "
                    "all content categories and formats."
                ),
            },
        },
        "options": {
            "jsonl_compact": {
                "requires": ["jsonl"],
                "description": (
                    "Share session metadata in one header per session; it only "
                    "changes JSONL and can be combined with Markdown or JSON."
                ),
            },
            "split_long_sessions": {
                "requires": ["markdown"],
                "description": "Split long Markdown files between messages.",
            },
            "include_raw_reasoning": {
                "description": (
                    "Include serialized reasoning blocks that have no readable text."
                ),
            },
        },
    }


def _parse_selection(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> SessionSelection:
    try:
        since = parse_since(args.since)
    except ValueError as error:
        parser.error(str(error))
    return SessionSelection(
        sessions=parse_session_ids(args.sessions),
        last=args.last,
        since=since,
        filter_text=args.filter or "",
        order=args.order,
    )


def _combined_filter_key(
    filter_key: str,
    selection: SessionSelection,
) -> str:
    payload = f"{filter_key}:{selection.key()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)

    source = args.source or args.source_pos
    read_only = args.check or args.list_sessions
    if args.json_output and not (read_only or args.capabilities):
        parser.error(
            "--json requires --check, --list-sessions, or --capabilities"
        )

    if args.capabilities:
        payload = _capabilities_payload()
        if args.json_output:
            _emit_json(payload)
        elif not args.quiet:
            print(f"copilot-export {__version__}")
            print(f"Formats: {', '.join(payload['formats'])}")
            print("Content:")
            for item in payload["content"]:
                print(f"  {item['name']}: {item['description']}")
            print("Presets:")
            for name, preset in payload["presets"].items():
                print(f"  {name}: {preset['description']}")
        return EXIT_OK

    selection = _parse_selection(args, parser)

    requested_formats = args.formats or ["markdown", "json"]
    formats = tuple("md" if fmt == "markdown" else fmt for fmt in requested_formats)
    formats = tuple(dict.fromkeys(formats))
    if not read_only and args.jsonl_compact and "jsonl" not in formats:
        parser.error(
            "--jsonl-compact requires JSONL output; use --format jsonl"
        )

    try:
        stderr_is_tty = sys.stderr.isatty()
    except (AttributeError, OSError):
        stderr_is_tty = False
    progress = Progress(
        enabled=not args.quiet
        and (
            args.progress
            or (
                not args.no_progress
                and stderr_is_tty
                and not read_only
            )
        )
    )

    try:
        sources = discover(source=source, db_path=args.db, progress=progress)
    except DiscoveryError as error:
        if read_only:
            return _readonly_error(
                args, _source_error_summary(source, args.db), str(error)
            )
        print(f"error: {error}", file=sys.stderr)
        return EXIT_SOURCE_ERROR

    try:
        assembled = assemble(sources, progress=progress)
    except DatabaseError as error:
        if read_only:
            return _readonly_error(
                args,
                _source_summary(sources, source, args.db),
                str(error),
            )
        print(f"error: {error}", file=sys.stderr)
        return EXIT_SOURCE_ERROR

    selected = select_sessions(
        assembled.sessions, selection, assembled.workspaces
    )

    if args.check:
        payload = _check_payload(sources, selected, selection, source, args.db)
        if args.json_output:
            _emit_json(payload)
        elif not args.quiet:
            print(f"Source environment: {sources.env}")
            print(
                "Stores: "
                + str(
                    len(sources.dbs)
                    + len(sources.jsonls)
                    + len(sources.agent_sessions)
                )
            )
            print(f"Sessions: {payload['session_count']}")
            print(f"Status: {payload['status']}")
            if payload["no_data_reason"]:
                print(f"No data reason: {payload['no_data_reason']}")
        return EXIT_OK if selected else EXIT_NO_DATA

    if args.list_sessions:
        payload = _list_payload(
            sources,
            selected,
            selection,
            assembled.workspaces,
            source,
            args.db,
        )
        if args.json_output:
            _emit_json(payload)
        elif not args.quiet:
            records = payload["sessions"]
            print(f"Sessions found: {len(records)}")
            for record in records:
                print(
                    f"{record['updated'] or '-'}  {record['id']}  "
                    f"{record['title'] or '(untitled)'}  "
                    f"[{record['workspace']}]"
                )
        return EXIT_OK if selected else EXIT_NO_DATA

    content = (
        {"prompts", "answers", "reasoning"}
        if args.conversation
        else set(args.content or CONTENT_TYPES)
    )
    try:
        turns = parse_turns(args.turns)
    except ValueError as error:
        parser.error(str(error))

    filter_options = FilterOptions(
        content=frozenset(content),
        turns=turns,
        last_turns=args.last_turns,
        omission_markers=args.omission_markers,
        max_reasoning_chars=args.max_reasoning_chars,
        include_raw_reasoning=args.include_raw_reasoning,
        max_tool_output_chars=args.max_tool_output_chars,
        truncate_direction=args.truncate_direction,
    )
    filter_key = _combined_filter_key(filter_options.key(), selection)
    sessions, filter_stats = filter_sessions(
        chronological(selected), filter_options, progress=progress
    )
    if not sessions:
        if not args.quiet:
            print("No chat records found.", file=sys.stderr)
        return EXIT_NO_DATA

    output_root = Path(args.output).expanduser()
    run_label = args.label or (
        "full"
        if content == set(CONTENT_TYPES)
        else "_".join(item for item in CONTENT_TYPES if item in content) or "none"
    )
    options = WriteOptions(
        output=output_root,
        formats=formats,
        env=sources.env,
        sources=_format_list(sources),
        max_session_md_bytes=(
            args.max_session_md_mb * 1_000_000
            if args.max_session_md_mb is not None
            else args.max_session_md_bytes
        ),
        split_long_sessions=args.split_long_sessions,
        jsonl_compact=args.jsonl_compact,
        run_label=run_label,
        filter_key=filter_key,
        filter_metadata={
            "key": filter_key,
            "content": sorted(content),
            "selection": selection.as_dict(),
            "turns": args.turns or ("all" if args.last_turns is None else None),
            "last_turns": args.last_turns,
            "omission_markers": args.omission_markers,
            "max_reasoning_chars": args.max_reasoning_chars,
            "include_raw_reasoning": args.include_raw_reasoning,
            "max_tool_output_chars": args.max_tool_output_chars,
            "truncate_direction": args.truncate_direction,
            "statistics": filter_stats.as_dict(),
        },
    )

    try:
        options.output = (
            find_run_directory(output_root, options.run_stamp, run_label)
            if args.dry_run
            else create_run_directory(output_root, options.run_stamp, run_label)
        )
    except OSError as error:
        print(f"error: cannot create output directory: {error}", file=sys.stderr)
        return EXIT_SOURCE_ERROR

    if args.dry_run:
        if not args.quiet:
            print(f"Source environment: {sources.env}")
            print(f"Sessions: {len(sessions)}")
            print(f"Output bundle: {options.output}")
            print(f"Formats: {', '.join(formats)}")
            print("Planned files:")
            for rel in plan_outputs(sessions, options, assembled.workspaces):
                print(f"  {rel}")
        return EXIT_OK

    try:
        result = write_exports(
            sessions, assembled.workspaces, options, progress=progress
        )
        write_metadata(options.output, sessions, options, result, assembled.workspaces)
    except OSError as error:
        print(f"error: cannot write output: {error}", file=sys.stderr)
        return EXIT_SOURCE_ERROR

    if not args.quiet:
        print(f"Source environment: {sources.env}")
        print(f"Sessions exported: {result.total_sessions}")
        print(f"Output bundle: {options.output.resolve()}")
        print(f"Written files: {len(result.written_files)}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
