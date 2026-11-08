"""Output formatting and one-bundle-per-run file writing."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from .models import ContentPart, Session, Message, utc_now_iso
from .progress import Progress

METADATA_NAME = "metadata.json"
DEFAULT_MAX_SESSION_MD_BYTES = 1_000_000

_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass
class WriteOptions:
    output: Path
    formats: Sequence[str]
    env: str = "linux"
    sources: List[str] = field(default_factory=list)
    filter_metadata: Dict[str, Any] = field(default_factory=dict)
    filter_key: str = ""
    split_long_sessions: bool = False
    max_session_md_bytes: int = DEFAULT_MAX_SESSION_MD_BYTES
    jsonl_compact: bool = False
    run_stamp: str = ""
    run_label: str = "full"

    def __post_init__(self) -> None:
        if not self.run_stamp:
            self.run_stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@dataclass
class RunResult:
    total_sessions: int = 0
    written_files: List[str] = field(default_factory=list)
    session_files: Dict[str, List[str]] = field(default_factory=dict)


def safe_component(text: str, limit: int = 80) -> str:
    text = _INVALID_FILENAME.sub("_", text or "")
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = "untitled"
    if text.upper() in _WINDOWS_RESERVED:
        text = "_" + text
    text = text[:limit].rstrip(" .")
    return text or "untitled"


def _session_date(session: Session) -> str:
    return session.created_at[:10]


def _workspace_name(workspace_id: str, workspaces: Dict[str, Any]) -> str:
    """Return a concise, human-readable workspace name for paths."""

    workspace = workspaces.get(workspace_id)
    raw_path = getattr(workspace, "path", "") if workspace is not None else ""
    if workspace_id == "global":
        return "global"
    normalised = str(raw_path).replace("\\", "/").rstrip("/")
    name = normalised.rsplit("/", 1)[-1] if normalised else ""
    # A missing workspace.json leaves the storage hash as its own path.  Do
    # not repeat that opaque value in the output name.
    if not name or name == workspace_id:
        name = "workspace"
    return safe_component(name, 48)


def _workspace_folders(
    sessions: List[Session], workspaces: Dict[str, Any]
) -> Dict[str, str]:
    """Name workspace folders by their most recently edited session.

    ISO dates remain human-readable and sort chronologically in any file
    manager.  The short storage hash is only a collision-safe suffix; it is
    not encryption and can be traced in ``metadata.json``.
    """

    latest: Dict[str, Session] = {}
    for session in sessions:
        previous = latest.get(session.workspace_id)
        if previous is None or session.updated_ts > previous.updated_ts:
            latest[session.workspace_id] = session
    return {
        workspace_id: (
            f"{session.updated_at[:10]}__{_workspace_name(workspace_id, workspaces)}"
            f"__{safe_component(workspace_id, 8)}"
        )
        for workspace_id, session in latest.items()
    }


def build_base_name(session: Session) -> str:
    title = safe_component(session.title, 80)
    return f"{_session_date(session)}__{title}"


def _workspace_label(session: Session, workspaces: Dict[str, Any]) -> str:
    workspace = workspaces.get(session.workspace_id)
    if workspace is None:
        return session.workspace_id
    return workspace.path or workspace.workspace_id


def _json_document(
    sessions: List[Session],
    workspaces: Dict[str, Any],
    env: str,
    export_date: str,
) -> Dict[str, Any]:
    session_documents = [
        session.as_dict(_workspace_label(session, workspaces)) for session in sessions
    ]
    for session_document, session in zip(session_documents, sessions):
        if session.usage:
            session_document["usage_summary"] = _usage_summary([session])
    return {
        "export_date": export_date,
        "source_env": env,
        "total_sessions": len(sessions),
        "sessions": session_documents,
    }


def _session_turn_count(session: Session) -> int:
    if session.selected_turn_count is not None:
        return session.selected_turn_count
    return max(
        max((message.turn for message in session.messages), default=0),
        sum(message.role == "user" for message in session.messages),
    )


def _usage_summary(sessions: Sequence[Session]) -> Dict[str, Any]:
    usage_records = [
        (session, record)
        for session in sessions
        for record in session.usage
    ]
    turn_count = sum(_session_turn_count(session) for session in sessions)
    by_field: Dict[str, List[int]] = {}
    for _session, record in usage_records:
        for name, field_data in record.fields.items():
            value = field_data.get("value")
            if isinstance(value, int) and not isinstance(value, bool):
                by_field.setdefault(name, []).append(value)

    if not usage_records:
        completeness = "unavailable"
    elif (
        len(usage_records) == turn_count
        and all(record.completeness == "complete" for _, record in usage_records)
    ):
        completeness = "complete"
    else:
        completeness = "partial"

    return {
        "source": "reported",
        "completeness": completeness,
        "turns_with_usage": len(usage_records),
        "turns_without_usage": max(0, turn_count - len(usage_records)),
        "turns_with_partial_usage": sum(
            record.completeness == "partial" for _, record in usage_records
        ),
        "totals": {name: sum(values) for name, values in sorted(by_field.items())},
        "coverage": {
            "total_turns": turn_count,
            "fields": {
                name: {
                    "reported_turns": len(values),
                    "selected_turns": turn_count,
                }
                for name, values in sorted(by_field.items())
            },
        },
    }


def _iter_jsonl_records(
    sessions: List[Session],
    workspaces: Dict[str, Any],
    compact: bool,
) -> Iterator[Dict[str, Any]]:
    for session in sessions:
        turn = 0
        workspace = _workspace_label(session, workspaces)
        if compact:
            yield {
                "type": "session",
                "session_id": session.session_id,
                "workspace": workspace,
                "workspace_id": session.workspace_id,
                "title": session.title,
                "agent": session.agent,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
            }
        for usage in session.usage:
            record = {
                "record_type": "usage",
                "session_id": session.session_id,
                "turn": usage.turn,
                "usage": usage.as_dict(),
            }
            if not compact:
                record.update(
                    {
                        "workspace": workspace,
                        "workspace_id": session.workspace_id,
                        "title": session.title,
                        "agent": session.agent,
                    }
                )
            yield record
        for message in session.messages:
            if message.turn:
                turn = message.turn
            elif message.role == "user":
                turn += 1
            if turn == 0:
                turn = 1
            parts = message.parts
            if not parts and message.content:
                kind = "prompt" if message.role == "user" else "answer"
                parts = [ContentPart(kind, message.content)]
            for index, part in enumerate(parts, start=1):
                record = {
                    "turn": turn,
                    "role": message.role,
                    "content_type": part.kind,
                    "part": index,
                    "timestamp": message.timestamp,
                    "content": part.content,
                    "truncated": part.truncated,
                    "original_chars": part.original_chars or len(part.content),
                }
                if compact:
                    record["type"] = "part"
                    record["session_id"] = session.session_id
                else:
                    record.update(
                        {
                            "session_id": session.session_id,
                            "workspace": workspace,
                            "workspace_id": session.workspace_id,
                            "title": session.title,
                            "agent": session.agent,
                            "created_at": session.created_at,
                            "updated_at": session.updated_at,
                        }
                    )
                yield record


def _jsonl_document(
    sessions: List[Session],
    workspaces: Dict[str, Any],
    compact: bool = False,
) -> str:
    """Return one JSON object per typed message fragment for embeddings."""

    lines = [
        json.dumps(record, ensure_ascii=False, sort_keys=True)
        for record in _iter_jsonl_records(sessions, workspaces, compact)
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if value is None or isinstance(value, (bool, int, float)):
        return json.dumps(value, ensure_ascii=False)
    raise TypeError(f"unsupported YAML front matter value: {type(value).__name__}")


def _yaml_mapping(mapping: Dict[str, Any], indent: int = 0) -> List[str]:
    lines: List[str] = []
    prefix = " " * indent
    for key, value in mapping.items():
        if isinstance(value, dict):
            if value:
                lines.append(f"{prefix}{key}:")
                lines.extend(_yaml_mapping(value, indent + 2))
            else:
                lines.append(f"{prefix}{key}: {{}}")
        else:
            lines.append(f"{prefix}{key}: {_yaml_scalar(value)}")
    return lines


def _markdown_metadata_block(session: Session, workspace_label: str) -> List[str]:
    metadata: Dict[str, Any] = {
        "session_id": session.session_id,
        "workspace": workspace_label,
        "agent": session.agent,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }
    if session.usage:
        metadata["usage"] = _usage_summary([session])
    return ["---", *_yaml_mapping(metadata), "---", ""]


def _format_markdown_message(message: Message) -> str:
    heading = {
        "user": "### User",
        "assistant": "### Assistant",
        "omission": "### Context omitted",
    }.get(message.role, f"### {message.role.title()}")
    return f"{heading}\n\n{message.content}\n"


def format_markdown_messages(messages: Iterable[Message]) -> str:
    return "\n".join(_format_markdown_message(message) for message in messages).rstrip() + "\n"


def _markdown_session_header(
    session: Session,
    workspace_label: str,
    include_document_header: bool = True,
    part_label: str = "",
) -> List[str]:
    title = session.title or "untitled"
    title = re.sub(r"[\r\n]+", " ", title).strip() or "untitled"
    lines = (
        _markdown_metadata_block(session, workspace_label)
        if include_document_header
        else []
    )
    lines.extend([f"# {session.created_at[:10]} — {title}", ""])
    if part_label:
        lines.extend([f"*{part_label}*", ""])
    return lines


def format_markdown_session(
    session: Session,
    workspace_label: str,
    include_document_header: bool = True,
) -> str:
    header = _markdown_session_header(
        session, workspace_label, include_document_header=include_document_header
    )
    body = format_markdown_messages(session.messages).rstrip()
    return "\n".join(header).rstrip() + "\n\n" + body + "\n"


def split_markdown_session(
    session: Session,
    workspace_label: str,
    max_bytes: int,
) -> List[str]:
    """Split Markdown at message boundaries without ever cutting a message."""

    full = format_markdown_session(session, workspace_label)
    if max_bytes <= 0 or len(full.encode("utf-8")) <= max_bytes or len(session.messages) <= 1:
        return [full]

    reserve = len(
        "\n".join(
            _markdown_session_header(
                session,
                workspace_label,
                part_label="Part 9999/9999",
            )
        ).encode("utf-8")
    ) + 4
    limit = max(1, max_bytes - reserve)
    blocks = [_format_markdown_message(message) for message in session.messages]
    chunks: List[List[str]] = []
    current: List[str] = []
    current_size = 0
    for block in blocks:
        block_size = len(block.encode("utf-8"))
        if current and current_size + block_size > limit:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(block)
        current_size += block_size
    if current:
        chunks.append(current)

    total = len(chunks)
    parts: List[str] = []
    for index, chunk in enumerate(chunks, start=1):
        header = _markdown_session_header(
            session,
            workspace_label,
            part_label=f"Part {index}/{total}",
        )
        body = "".join(chunk).rstrip()
        parts.append("\n".join(header).rstrip() + "\n\n" + body + "\n")
    return parts


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _unique_bases(
    sessions: List[Session], workspace_folders: Dict[str, str]
) -> Dict[str, str]:
    counts = Counter(
        (workspace_folders[session.workspace_id], build_base_name(session))
        for session in sessions
    )
    bases: Dict[str, str] = {}
    for session in sessions:
        folder = workspace_folders[session.workspace_id]
        base = build_base_name(session)
        if counts[(folder, base)] > 1:
            base = f"{base}__{session.session_id[:8]}"
        bases[session.session_id] = base
    return bases


def _relative(path: Path, output: Path) -> str:
    try:
        return path.relative_to(output).as_posix()
    except ValueError:
        return str(path)


def _write_jsonl_bundle(
    sessions: List[Session],
    workspaces: Dict[str, Any],
    options: WriteOptions,
    result: RunResult,
    progress: Optional[Progress] = None,
) -> None:
    """Write a single JSONL stream for this export run."""

    target = options.output / "history.jsonl"
    _write_text(target, _jsonl_document(sessions, workspaces, options.jsonl_compact))
    relative = _relative(target, options.output)
    result.written_files.append(relative)
    for session in sessions:
        result.session_files.setdefault(session.session_id, []).append(relative)
    if progress is not None:
        progress.step("history.jsonl")


def plan_outputs(
    sessions: List[Session],
    options: WriteOptions,
    workspaces: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Return all bundle-relative paths that an export run will write."""

    formats = [fmt for fmt in options.formats if fmt != "jsonl"]
    planned: List[str] = []
    workspace_folders = _workspace_folders(sessions, workspaces or {})
    bases = _unique_bases(sessions, workspace_folders)
    for session in sessions:
        folder = workspace_folders[session.workspace_id]
        base = bases[session.session_id]
        for fmt in formats:
            if fmt == "md" and options.split_long_sessions:
                parts = split_markdown_session(
                    session,
                    _workspace_label(session, workspaces or {}),
                    options.max_session_md_bytes,
                )
                if len(parts) > 1:
                    planned.extend(
                        f"{folder}/{base}__part{index:02d}.md"
                        for index in range(1, len(parts) + 1)
                    )
                    continue
            planned.append(f"{folder}/{base}.{fmt}")
    if "jsonl" in options.formats:
        planned.append("history.jsonl")
    planned.append(METADATA_NAME)
    return planned


def run_directory_name(run_stamp: str, label: str) -> str:
    """Build a readable, filesystem-safe directory name for one export run."""

    return f"{safe_component(run_stamp, 32)}_{safe_component(label, 48)}"


def find_run_directory(root: Path, run_stamp: str, label: str) -> Path:
    """Return an unused run path without creating it (used by dry-run)."""

    if root.exists() and not root.is_dir():
        raise NotADirectoryError(f"output root is not a directory: {root}")
    base_name = run_directory_name(run_stamp, label)
    candidate = root / base_name
    counter = 2
    while candidate.exists():
        candidate = root / f"{base_name}_{counter}"
        counter += 1
    return candidate


def create_run_directory(root: Path, run_stamp: str, label: str) -> Path:
    """Atomically create a unique directory for this export run."""

    root.mkdir(parents=True, exist_ok=True)
    base_name = run_directory_name(run_stamp, label)
    counter = 1
    while True:
        suffix = "" if counter == 1 else f"_{counter}"
        candidate = root / f"{base_name}{suffix}"
        try:
            candidate.mkdir()
        except FileExistsError:
            counter += 1
            continue
        return candidate


def write_exports(
    sessions: List[Session],
    workspaces: Dict[str, Any],
    options: WriteOptions,
    progress: Optional[Progress] = None,
) -> RunResult:
    result = RunResult(total_sessions=len(sessions))
    if not sessions:
        return result

    session_formats = tuple(fmt for fmt in options.formats if fmt != "jsonl")
    progress_total = (len(sessions) if session_formats else 0) + int(
        "jsonl" in options.formats
    )
    if progress is not None:
        progress.start("Writing exports", total=progress_total)

    try:
        if session_formats:
            workspace_folders = _workspace_folders(sessions, workspaces)
            bases = _unique_bases(sessions, workspace_folders)
            for session in sessions:
                if progress is not None:
                    progress.step(session.title or session.session_id)
                folder = workspace_folders[session.workspace_id]
                base = bases[session.session_id]
                session_files = result.session_files.setdefault(session.session_id, [])
                workspace_label = _workspace_label(session, workspaces)
                for fmt in session_formats:
                    if fmt == "md" and options.split_long_sessions:
                        parts = split_markdown_session(
                            session, workspace_label, options.max_session_md_bytes
                        )
                    else:
                        parts = []
                    if parts and len(parts) > 1:
                        paths = [
                            options.output / folder / f"{base}__part{index:02d}.md"
                            for index in range(1, len(parts) + 1)
                        ]
                        for target, content in zip(paths, parts):
                            _write_text(target, content)
                    else:
                        target = options.output / folder / f"{base}.{fmt}"
                        if fmt == "json":
                            content = json.dumps(
                                _json_document(
                                    [session], workspaces, options.env, utc_now_iso()
                                ),
                                ensure_ascii=False,
                                indent=2,
                            ) + "\n"
                        else:
                            content = parts[0] if parts else format_markdown_session(
                                session, workspace_label, include_document_header=True
                            )
                        _write_text(target, content)
                        paths = [target]
                    for target in paths:
                        relative = _relative(target, options.output)
                        result.written_files.append(relative)
                        session_files.append(relative)

        if "jsonl" in options.formats:
            _write_jsonl_bundle(sessions, workspaces, options, result, progress)
        return result
    finally:
        if progress is not None:
            progress.done()


def write_metadata(
    output: Path,
    sessions: List[Session],
    options: WriteOptions,
    result: RunResult,
    workspaces: Dict[str, Any],
) -> Path:
    metadata = {
        "export_date": utc_now_iso(),
        "run_id": output.name,
        "run_label": options.run_label,
        "source_env": options.env,
        "total_sessions": len(sessions),
        "formats": list(options.formats),
        "filters": options.filter_metadata,
        "usage": _usage_summary(sessions),
        "filter_key": options.filter_key,
        "layout": {
            "split_long_sessions": options.split_long_sessions,
            "max_session_md_bytes": options.max_session_md_bytes,
            "jsonl_compact": options.jsonl_compact,
        },
        "output_dir": str(output),
        "sources": options.sources,
        "session_files": result.session_files,
        "written_files": result.written_files,
        "workspaces": {
            workspace_id: workspace.path or workspace_id
            for workspace_id, workspace in workspaces.items()
        },
    }
    path = output / METADATA_NAME
    _write_text(
        path,
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return path
