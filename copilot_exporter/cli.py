"""Command line interface for the  vscode-copilot-export-cli"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__
from .db import DatabaseError
from .discovery import DiscoveryError, discover
from .filters import CONTENT_TYPES, FilterOptions, filter_sessions, parse_turns
from .normalize import assemble
from .progress import Progress
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
    parser.add_argument(
        "-o",
        "--output",
        default="./copilot_export",
        help="Root directory for timestamped export bundles (default: ./copilot_export).",
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)

    source = args.source or args.source_pos
    requested_formats = args.formats or ["markdown", "json"]
    formats = tuple("md" if fmt == "markdown" else fmt for fmt in requested_formats)
    formats = tuple(dict.fromkeys(formats))
    if args.jsonl_compact and "jsonl" not in formats:
        parser.error(
            "--jsonl-compact requires JSONL output; use --format jsonl"
        )
    content = (
        {"prompts", "answers", "reasoning"}
        if args.conversation
        else set(args.content or CONTENT_TYPES)
    )
    try:
        turns = parse_turns(args.turns)
    except ValueError as error:
        parser.error(str(error))

    try:
        stderr_is_tty = sys.stderr.isatty()
    except (AttributeError, OSError):
        stderr_is_tty = False
    progress = Progress(
        enabled=not args.quiet
        and (args.progress or (not args.no_progress and stderr_is_tty))
    )

    try:
        sources = discover(source=source, db_path=args.db, progress=progress)
    except DiscoveryError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_SOURCE_ERROR

    try:
        assembled = assemble(sources, progress=progress)
    except DatabaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_SOURCE_ERROR

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
    filter_key = filter_options.key()
    sessions, filter_stats = filter_sessions(
        assembled.sessions, filter_options, progress=progress
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
