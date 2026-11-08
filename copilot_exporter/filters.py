"""Content, turn and truncation filters applied before output is written."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Set, Tuple

from .models import ContentPart, Message, Session
from .progress import Progress

CONTENT_TYPES = (
    "prompts",
    "answers",
    "reasoning",
    "tool-calls",
    "tool-results",
    "edits",
    "references",
    "tool-events",
)
_PART_TO_OPTION = {
    "prompt": "prompts",
    "answer": "answers",
    "reasoning": "reasoning",
    "tool_call": "tool-calls",
    "tool_result": "tool-results",
    "tool_output": "tool-results",
    "edit": "edits",
    "reference": "references",
    "inline_reference": "references",
    "tool_event": "tool-events",
}

_TRUNCATE_BEGINNING = "beginning"
_TRUNCATE_END = "end"
_CODE_FENCE_OPEN = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_CODE_FENCE_CLOSE = re.compile(r"^[ \t]{0,3}(?P<fence>`+|~+)[ \t]*$")
_TOOL_PARTS = frozenset(
    {"tool_call", "tool_result", "tool_output", "edit", "reference", "tool_event"}
)


@dataclass(frozen=True)
class FilterOptions:
    content: frozenset[str] = frozenset(CONTENT_TYPES)
    turns: Optional[frozenset[int]] = None
    last_turns: Optional[int] = None
    omission_markers: bool = True
    max_reasoning_chars: Optional[int] = None
    include_raw_reasoning: bool = False
    max_tool_output_chars: Optional[int] = None
    truncate_direction: str = _TRUNCATE_END

    def key(self) -> str:
        """Return a stable identifier for this exact filter variant."""

        payload = {
            "content": sorted(self.content),
            "turns": sorted(self.turns) if self.turns is not None else None,
            "last_turns": self.last_turns,
            "omission_markers": self.omission_markers,
            "max_reasoning_chars": self.max_reasoning_chars,
            "include_raw_reasoning": self.include_raw_reasoning,
            "max_tool_output_chars": self.max_tool_output_chars,
            "truncate_direction": self.truncate_direction,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


@dataclass
class FilterStats:
    available_turns: int = 0
    selected_turns: int = 0
    emitted_parts: dict[str, int] = field(default_factory=dict)
    excluded_parts: dict[str, int] = field(default_factory=dict)
    original_chars: dict[str, int] = field(default_factory=dict)
    exported_chars: dict[str, int] = field(default_factory=dict)
    truncated_parts: dict[str, int] = field(default_factory=dict)
    omissions: List[dict] = field(default_factory=list)

    def add(self, bucket: str, part: ContentPart, emitted: bool) -> None:
        target = self.emitted_parts if emitted else self.excluded_parts
        target[bucket] = target.get(bucket, 0) + 1
        self.original_chars[bucket] = self.original_chars.get(bucket, 0) + len(part.content)
        if emitted:
            self.exported_chars[bucket] = self.exported_chars.get(bucket, 0) + len(part.content)

    def mark_truncated(self, bucket: str, original_length: int, exported_length: int) -> None:
        self.truncated_parts[bucket] = self.truncated_parts.get(bucket, 0) + 1
        self.original_chars[bucket] = self.original_chars.get(bucket, 0) - exported_length + original_length

    def as_dict(self) -> dict:
        return {
            "available_turns": self.available_turns,
            "selected_turns": self.selected_turns,
            "parts": {
                "emitted": self.emitted_parts,
                "excluded": self.excluded_parts,
                "truncated": self.truncated_parts,
            },
            "characters": {
                "original": self.original_chars,
                "exported": self.exported_chars,
            },
            "omissions": self.omissions,
        }


def parse_turns(specifications: Iterable[str]) -> Optional[frozenset[int]]:
    """Parse one-based selections such as ``1-3,7,10-``."""

    selected: Set[int] = set()
    has_specification = False
    for specification in specifications:
        for item in specification.split(","):
            item = item.strip()
            if not item:
                continue
            has_specification = True
            if "-" not in item:
                try:
                    value = int(item)
                except ValueError as error:
                    raise ValueError(f"invalid turn selection: {item}") from error
                if value < 1:
                    raise ValueError("turn numbers start at 1")
                selected.add(value)
                continue
            start_text, end_text = item.split("-", 1)
            try:
                start = int(start_text)
            except ValueError as error:
                raise ValueError(f"invalid turn range: {item}") from error
            if start < 1:
                raise ValueError("turn numbers start at 1")
            if not end_text:
                # A large sentinel keeps parsing simple; unavailable turns are
                # naturally ignored when sessions are filtered.
                selected.update(range(start, 1_000_001))
                continue
            try:
                end = int(end_text)
            except ValueError as error:
                raise ValueError(f"invalid turn range: {item}") from error
            if end < start:
                raise ValueError(f"invalid turn range: {item}")
            selected.update(range(start, end + 1))
    return frozenset(selected) if has_specification else None


def _parts_for(message: Message) -> List[ContentPart]:
    if message.parts:
        return message.parts
    fallback = "prompt" if message.role == "user" else "answer"
    return [ContentPart(fallback, message.content)] if message.content else []


def _inline_reference_label(part: ContentPart) -> str:
    if part.kind == "inline_reference":
        return part.label
    if part.kind != "reference" or part.label != "inlineReference":
        return ""
    try:
        payload = json.loads(part.content)
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    reference = payload.get("inlineReference")
    if not isinstance(reference, dict):
        reference = payload
    name = reference.get("name")
    return name.strip() if isinstance(name, str) else ""


def _truncate(
    part: ContentPart,
    limit: Optional[int],
    direction: str = _TRUNCATE_END,
) -> ContentPart:
    if limit is None or len(part.content) <= limit:
        return deepcopy(part)
    if direction not in (_TRUNCATE_BEGINNING, _TRUNCATE_END):
        raise ValueError(f"invalid truncation direction: {direction}")
    original_length = len(part.content)
    kept = limit
    retained = "first" if direction == _TRUNCATE_BEGINNING else "last"
    marker = f"[... truncated {original_length - kept} characters; kept {retained} {kept}]"
    excerpt = (
        part.content[:kept]
        if direction == _TRUNCATE_BEGINNING
        else part.content[-kept:] if kept else ""
    )
    content = (
        f"{excerpt}\n\n{marker}"
        if direction == _TRUNCATE_BEGINNING
        else f"{marker}\n\n{excerpt}"
    )
    return ContentPart(
        kind=part.kind,
        content=content,
        label=part.label,
        truncated=True,
        original_chars=len(part.content),
    )


def render_message(parts: List[ContentPart], role: str) -> str:
    if role == "user":
        return "\n\n".join(part.content for part in parts).strip()
    if role == "omission":
        return parts[0].content if parts else ""

    rendered: List[str] = []
    text_run = ""
    previous_was_reference = False

    def flush_text_run() -> None:
        nonlocal text_run, previous_was_reference
        if text_run:
            rendered.append(text_run.strip())
        text_run = ""
        previous_was_reference = False

    headings = {
        "reasoning": "Reasoning",
        "tool_call": "Tool call/input",
        "tool_result": "Tool result",
        "tool_output": "Tool result",
        "edit": "Edit/diff",
        "reference": "Reference",
        "tool_event": "Tool event",
    }
    for part in parts:
        if part.kind == "answer":
            if text_run and not previous_was_reference and not text_run[-1].isspace() and not part.content[:1].isspace():
                text_run += "\n\n"
            text_run += part.content
            previous_was_reference = False
            continue
        inline_label = _inline_reference_label(part)
        if inline_label or part.kind == "inline_reference":
            reference = f"`{inline_label}`" if inline_label else ""
            if reference:
                text_run += reference
                previous_was_reference = True
            continue

        flush_text_run()
        heading = headings.get(part.kind)
        if part.kind == "omission":
            rendered.append(f"> {part.content}")
        elif heading == "Reasoning":
            rendered.append(f"## {heading}\n\n{part.content}")
        elif heading:
            body = _markdown_part_content(part)
            if body:
                label = f" — {part.label}" if part.label else ""
                rendered.append(f"### {heading}{label}\n\n{body}")
        else:
            rendered.append(part.content)
    flush_text_run()
    return "\n\n".join(item for item in rendered if item).strip()


def _nested_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(text for item in value if (text := _nested_text(item)))
    if isinstance(value, dict):
        for key in ("value", "text", "content", "markdown", "message", "command", "output"):
            if key in value:
                text = _nested_text(value[key])
                if text:
                    return text
    return ""


def _is_opaque_reasoning(part: ContentPart) -> bool:
    if part.kind != "reasoning":
        return False
    try:
        payload = json.loads(part.content)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("kind") == "thinking"
        and not _nested_text(payload.get("value"))
    )


def _result_detail_text(value: object) -> str:
    if not isinstance(value, list):
        return _nested_text(value)
    rendered: List[str] = []
    for detail in value:
        if not isinstance(detail, dict):
            text = _nested_text(detail)
            if text:
                rendered.append(f"- {text}")
            continue
        uri = detail.get("uri")
        if isinstance(uri, dict):
            path = uri.get("fsPath") or uri.get("path") or uri.get("external")
        else:
            path = uri
        location = detail.get("location")
        range_data = detail.get("range")
        if not isinstance(range_data, dict) and isinstance(location, dict):
            range_data = location.get("range")
        text_parts: List[str] = []
        if isinstance(path, str) and path:
            text_parts.append(path)
        if isinstance(range_data, dict):
            start = range_data.get("startLineNumber")
            end = range_data.get("endLineNumber")
            if isinstance(start, int) and start > 0:
                text_parts.append(f"L{start}" if end in (None, start) else f"L{start}-L{end}")
        if not text_parts:
            text = _nested_text(detail)
            if text:
                text_parts.append(text)
        if text_parts:
            rendered.append(f"- {':'.join(text_parts)}")
    return "\n".join(rendered)


def _tool_result_text(value: object) -> str:
    if not isinstance(value, dict):
        return _nested_text(value)
    sections: List[str] = []
    for key, item in value.items():
        normalized = key.lower()
        if normalized == "resultdetails":
            text = _result_detail_text(item)
        elif normalized == "toolspecificdata":
            text = _tool_result_text(item)
        elif normalized in {
            "pasttensemessage",
            "result",
            "output",
            "resulttext",
            "toolresult",
        }:
            text = _nested_text(item)
        else:
            continue
        if text:
            sections.append(text)
    return "\n\n".join(sections)


def _markdown_part_content(part: ContentPart) -> str:
    try:
        payload = json.loads(part.content)
    except (TypeError, ValueError):
        return part.content
    if not isinstance(payload, dict):
        return part.content

    if part.kind == "tool_result":
        text = _tool_result_text(payload)
        if text:
            return text
    elif part.kind == "tool_call":
        details: List[str] = []
        for key in (
            "invocationMessage",
            "input",
            "arguments",
            "params",
            "command",
            "code",
            "toolSpecificData",
        ):
            if key in payload:
                text = _nested_text(payload[key])
                if text and text not in details:
                    details.append(text)
        if details:
            return "\n\n".join(details)
        call_id = payload.get("toolCallId")
        if isinstance(call_id, str) and call_id:
            return f"Call details were not stored (toolCallId: `{call_id}`)."
    elif part.kind == "tool_event":
        for key in ("message", "value", "text", "title", "pastTenseMessage"):
            if key in payload:
                text = _nested_text(payload[key])
                if text:
                    return text
        return ""
    elif part.kind == "reference":
        for key in ("references", "uri", "value", "text", "name"):
            if key in payload:
                text = _nested_text(payload[key])
                if text:
                    return text
        return ""

    return f"```text\n{part.content}\n```"


def _remove_empty_code_fences(content: str) -> str:
    lines = content.splitlines(keepends=True)
    cleaned: List[str] = []
    index = 0
    while index < len(lines):
        opening = _CODE_FENCE_OPEN.fullmatch(lines[index].rstrip("\r\n"))
        if opening is None:
            cleaned.append(lines[index])
            index += 1
            continue

        fence = opening.group("fence")
        if fence.startswith("`") and "`" in opening.group("info"):
            cleaned.append(lines[index])
            index += 1
            continue

        closing_index: Optional[int] = None
        for candidate_index in range(index + 1, len(lines)):
            closing = _CODE_FENCE_CLOSE.fullmatch(
                lines[candidate_index].rstrip("\r\n")
            )
            if (
                closing is not None
                and closing.group("fence")[0] == fence[0]
                and len(closing.group("fence")) >= len(fence)
            ):
                closing_index = candidate_index
                break

        if closing_index is None:
            cleaned.append(lines[index])
            index += 1
        elif all(not line.strip() for line in lines[index + 1 : closing_index]):
            index = closing_index + 1
        else:
            cleaned.extend(lines[index : closing_index + 1])
            index = closing_index + 1

    content = "".join(cleaned)
    return re.sub(r"\n{3,}", "\n\n", content).strip()


def _omission_hint(part: ContentPart) -> str:
    if part.label and part.label != "toolInvocationSerialized":
        return part.label
    try:
        value = json.loads(part.content)
    except (TypeError, ValueError):
        return ""
    if not isinstance(value, dict):
        return ""
    keys = ("toolName", "name", "command", "title", "invocationMessage", "uri")
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, dict):
            candidate = candidate.get("value") or candidate.get("text")
        if isinstance(candidate, str) and candidate.strip():
            return re.sub(r"\s+", " ", candidate.strip())[:72]
    return ""


def _append_content_omission(
    stats: FilterStats,
    session: Session,
    message: Message,
    message_index: int,
    turn: int,
    omissions: List[Tuple[int, str, ContentPart]],
) -> ContentPart:
    buckets = list(dict.fromkeys(bucket for _, bucket, _ in omissions))
    chars = sum(len(part.content) for _, _, part in omissions)
    hints = list(
        dict.fromkeys(hint for _, _, part in omissions if (hint := _omission_hint(part)))
    )
    categories = ", ".join(buckets)
    hint = "; ".join(hints[:2])
    marker = f"{categories} omitted"
    if hint:
        marker += f": {hint}"
    marker += f" ({chars:,} original characters)"
    stats.omissions.append(
        {
            "session_id": session.session_id,
            "turn": turn,
            "timestamp": message.timestamp,
            "message_index": message_index,
            "part_indices": [index for index, _, _ in omissions],
            "categories": buckets,
            "original_chars": chars,
            "hints": hints[:5],
        }
    )
    return ContentPart("omission", marker, original_chars=chars)


def filter_sessions(
    sessions: Iterable[Session],
    options: FilterOptions,
    progress: Optional[Progress] = None,
) -> Tuple[List[Session], FilterStats]:
    """Return filtered copies, preserving user/assistant turns as a unit."""

    stats = FilterStats()
    result: List[Session] = []
    session_list = list(sessions)
    if progress is not None:
        progress.start("Filtering sessions", total=len(session_list))
    for original in session_list:
        if progress is not None:
            progress.step(original.title or original.session_id)
        session = deepcopy(original)
        messages: List[Message] = []
        total_turns = max(
            max((message.turn for message in original.messages), default=0),
            sum(message.role == "user" for message in original.messages),
        )
        last_turn_start = (
            max(1, total_turns - options.last_turns + 1)
            if options.last_turns is not None
            else 1
        )
        turn = 0
        selected_in_session: Set[int] = set()
        omitted_turn_start: Optional[int] = None
        for message_index, original_message in enumerate(original.messages, start=1):
            if original_message.role == "user":
                turn = original_message.turn or turn + 1
            elif original_message.turn:
                turn = original_message.turn
            if turn == 0:
                turn = 1
            turn_selected = (
                options.turns is None or turn in options.turns
            ) and (options.last_turns is None or turn >= last_turn_start)
            if not turn_selected:
                if omitted_turn_start is None:
                    omitted_turn_start = turn
                for part_index, part in enumerate(_parts_for(original_message), start=1):
                    stats.add(_PART_TO_OPTION.get(part.kind, "answers"), part, emitted=False)
                    if part.kind in _TOOL_PARTS:
                        stats.omissions.append(
                            {
                                "session_id": session.session_id,
                                "turn": turn,
                                "timestamp": original_message.timestamp,
                                "message_index": message_index,
                                "part_indices": [part_index],
                                "categories": [_PART_TO_OPTION.get(part.kind, "answers")],
                                "original_chars": len(part.content),
                                "hints": [hint] if (hint := _omission_hint(part)) else [],
                                "reason": "turn-filter",
                            }
                        )
                continue
            if omitted_turn_start is not None:
                omitted_end = turn - 1
                omission = _turn_omission(omitted_turn_start, omitted_end)
                messages.append(
                    Message(
                        "omission",
                        omission.content,
                        original_message.timestamp,
                        original_message.sort_key,
                        parts=[omission],
                        turn=turn,
                    )
                )
                omitted_turn_start = None
            if turn_selected:
                selected_in_session.add(turn)
            emitted: List[ContentPart] = []
            pending_omissions: List[Tuple[int, str, ContentPart]] = []
            omitted_tool_content = False
            for part_index, part in enumerate(_parts_for(original_message), start=1):
                bucket = _PART_TO_OPTION.get(part.kind, "answers")
                selected_categories = options.content
                inline_reference = (
                    part.kind == "inline_reference"
                    or (
                        part.kind == "reference"
                        and part.label == "inlineReference"
                    )
                )
                opaque_reasoning = _is_opaque_reasoning(part)
                include_part = (
                    inline_reference
                    or (
                        bucket in selected_categories
                        and (
                            options.include_raw_reasoning
                            or not opaque_reasoning
                        )
                    )
                )
                if not include_part:
                    stats.add(bucket, part, emitted=False)
                    if part.kind in _TOOL_PARTS:
                        omitted_tool_content = True
                        pending_omissions.append((part_index, bucket, part))
                    elif opaque_reasoning and bucket in selected_categories:
                        pending_omissions.append(
                            (part_index, "serialized_reasoning", part)
                        )
                    else:
                        if pending_omissions:
                            omission_marker = _append_content_omission(
                                stats,
                                session,
                                original_message,
                                message_index,
                                turn,
                                pending_omissions,
                            )
                            if options.omission_markers:
                                emitted.append(omission_marker)
                            pending_omissions.clear()
                    continue
                if pending_omissions:
                    omission_marker = _append_content_omission(
                        stats,
                        session,
                        original_message,
                        message_index,
                        turn,
                        pending_omissions,
                    )
                    if options.omission_markers:
                        emitted.append(omission_marker)
                    pending_omissions.clear()
                limit = None
                if part.kind == "reasoning":
                    limit = options.max_reasoning_chars
                elif part.kind in {"tool_result", "tool_output"}:
                    limit = options.max_tool_output_chars
                rendered = _truncate(part, limit, options.truncate_direction)
                stats.add(bucket, rendered, emitted=True)
                if rendered.truncated:
                    stats.mark_truncated(bucket, len(part.content), len(rendered.content))
                emitted.append(rendered)
            if pending_omissions:
                omission_marker = _append_content_omission(
                    stats,
                    session,
                    original_message,
                    message_index,
                    turn,
                    pending_omissions,
                )
                if options.omission_markers:
                    emitted.append(omission_marker)
            content = render_message(emitted, original_message.role)
            if (
                content
                and omitted_tool_content
                and not options.omission_markers
                and original_message.role == "assistant"
            ):
                content = _remove_empty_code_fences(content)
            if content:
                messages.append(
                    Message(
                        original_message.role,
                        content,
                        original_message.timestamp,
                        original_message.sort_key,
                        parts=emitted,
                        turn=turn,
                    )
                )
        if omitted_turn_start is not None and selected_in_session:
            omission = _turn_omission(omitted_turn_start, turn)
            messages.append(
                Message(
                    "omission",
                    omission.content,
                    parts=[omission],
                    turn=turn,
                )
            )
        session.usage = [
            record
            for record in original.usage
            if (
                options.turns is None or record.turn in options.turns
            )
            and (
                options.last_turns is None or record.turn >= last_turn_start
            )
        ]
        session.selected_turn_count = len(selected_in_session)
        stats.available_turns += turn
        stats.selected_turns += len(selected_in_session)
        if messages:
            session.messages = messages
            result.append(session)
    if progress is not None:
        progress.done()
    return result, stats


def _turn_omission(start: int, end: int) -> ContentPart:
    turns = f"turn {start}" if start == end else f"turns {start}-{end}"
    return ContentPart(
        "omission",
        f"{turns} omitted by turn filter to preserve chronological context",
    )
