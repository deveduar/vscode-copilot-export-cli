"""Parsers for VS Code SQLite rows and Copilot JSONL session logs."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import AGENT_DEFAULT
from .db import read_agent_session_rows
from .models import (
    ContentPart,
    Message,
    Session,
    UsageRecord,
    Workspace,
    epoch_to_iso,
    guess_title,
    to_epoch,
)
from .filters import render_message

_TEXT_KINDS = {None, "", "text", "markdown", "markdownContent", "response", "content"}
_EDIT_KINDS = {"textEditGroup"}
_REFERENCE_KINDS = {"inlineReference", "codeblockUri", "references"}
_CALL_KINDS = {"toolInvocationSerialized"}
_OTHER_TOOL_KINDS = {
    "undoStop",
    "mcpServersStarting",
    "progressTaskSerialized",
    "progressMessage",
    "confirmation",
    "warning",
    "error",
}


@dataclass
class SessionMeta:
    session_id: str
    title: str = ""
    created_ts: Optional[float] = None
    updated_ts: Optional[float] = None


@dataclass
class PromptHistory:
    prompts: List[str] = field(default_factory=list)
    created_ts: Optional[float] = None
    key: str = ""


@dataclass
class DbExtraction:
    sessions: List[Session] = field(default_factory=list)
    index: Dict[str, SessionMeta] = field(default_factory=dict)
    prompt_histories: List[PromptHistory] = field(default_factory=list)


@dataclass
class AgentSessionExtraction:
    session: Session
    working_directory: str = ""
    turn_count: int = 0


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _part_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("value", "text", "content", "markdown"):
            nested = value.get(key)
            if isinstance(nested, str):
                return nested
            if isinstance(nested, (dict, list)):
                text = _part_text(nested)
                if text:
                    return text
    if isinstance(value, list):
        return "\n".join(filter(None, (_part_text(item) for item in value)))
    return ""


def extract_user_text(request: Dict[str, Any]) -> str:
    message = request.get("message")
    if isinstance(message, str):
        return _clean_text(message)
    if isinstance(message, dict):
        text = _clean_text(message.get("text"))
        if text:
            return text
        parts = message.get("parts")
        if isinstance(parts, list):
            chunks = []
            for part in parts:
                if isinstance(part, dict):
                    chunk = _clean_text(part.get("text") or part.get("value"))
                    if chunk:
                        chunks.append(chunk)
                elif isinstance(part, str):
                    chunk = _clean_text(part)
                    if chunk:
                        chunks.append(chunk)
            if chunks:
                return "\n".join(chunks)
        text = _part_text(message.get("value"))
        if text:
            return text.strip()
    for key in ("prompt", "query", "inputText"):
        text = _clean_text(request.get(key))
        if text:
            return text
    return ""


def extract_response_parts(response: Any) -> List[ContentPart]:
    """Return visible text plus stored reasoning and tool events.

    VS Code stores a response as heterogeneous parts.  Older versions of the
    exporter intentionally discarded non-rendered parts, which also discarded
    useful ``thinking`` text and serialized tool results.  Keep them in the
    assistant message under explicit Markdown sections; this retains the
    public JSON schema (only ``user`` and ``assistant`` roles) while making
    the additional data searchable and readable.
    """

    if isinstance(response, str):
        text = _clean_text(response)
        return [ContentPart("answer", text)] if text else []
    if isinstance(response, dict):
        parts: List[Any] = [response]
    elif isinstance(response, list):
        parts = response
    else:
        return []

    result: List[ContentPart] = []
    for part in parts:
        if isinstance(part, str):
            if part.strip():
                text = part
                result.append(ContentPart("answer", text))
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("kind")
        if kind == "thinking":
            text = _part_text(part.get("value")).strip()
            if text:
                result.append(ContentPart("reasoning", text))
            else:
                result.append(_serialized_part("reasoning", kind, part))
            continue
        if kind in _CALL_KINDS:
            result.extend(_tool_invocation_parts(part))
            continue
        if kind in _EDIT_KINDS:
            result.append(_serialized_part("edit", kind, part))
            continue
        if kind == "inlineReference":
            result.append(
                ContentPart(
                    "inline_reference",
                    json.dumps(part, ensure_ascii=False, indent=2, default=str),
                    label=_inline_reference_label(part),
                )
            )
            continue
        if kind in _REFERENCE_KINDS:
            result.append(_serialized_part("reference", kind, part))
            continue
        if kind in _OTHER_TOOL_KINDS:
            result.append(_serialized_part("tool_event", kind, part))
            continue
        if kind not in _TEXT_KINDS:
            result.append(_serialized_part("tool_event", kind or "unknown", part))
            continue
        text = _part_text(part.get("value"))
        if not text and "text" in part:
            text = _part_text(part.get("text"))
        if text.strip():
            result.append(ContentPart("answer", text))
    return result


def _inline_reference_label(part: Dict[str, Any]) -> str:
    reference = part.get("inlineReference")
    if not isinstance(reference, dict):
        reference = part
    name = reference.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()

    location = reference.get("location")
    uri = location.get("uri") if isinstance(location, dict) else None
    if not isinstance(uri, dict):
        uri = reference.get("uri")
    if not isinstance(uri, dict) and any(
        key in reference for key in ("fsPath", "path", "external")
    ):
        uri = reference
    if isinstance(uri, dict):
        raw_path = uri.get("fsPath") or uri.get("path") or uri.get("external")
    else:
        raw_path = uri
    if not isinstance(raw_path, str) or not raw_path:
        return ""
    normalized_path = raw_path.replace("\\", "/").rstrip("/")
    filename = normalized_path.rsplit("/", 1)[-1]
    range_data = location.get("range") if isinstance(location, dict) else None
    if isinstance(range_data, dict):
        line = range_data.get("startLineNumber")
        if isinstance(line, int) and line > 0:
            return f"{filename}:{line}"
    return filename


def _serialized_part(category: str, kind: Any, value: Dict[str, Any]) -> ContentPart:
    return ContentPart(
        category,
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        label=str(kind),
    )


def _tool_invocation_parts(value: Dict[str, Any]) -> List[ContentPart]:
    result: List[ContentPart] = []
    call_keys = {
        "invocationmessage",
        "toolname",
        "input",
        "arguments",
        "params",
    }
    result_keys = {
        "result",
        "output",
        "resulttext",
        "toolresult",
        "pasttensemessage",
        "resultdetails",
    }
    call_data = {
        key: item for key, item in value.items() if key.lower() in call_keys
    }
    result_data = {
        key: item for key, item in value.items() if key.lower() in result_keys
    }
    event_data = {
        key: item
        for key, item in value.items()
        if key.lower() not in call_keys
        and key.lower() not in result_keys
        and key != "kind"
    }

    specific_data = event_data.get("toolSpecificData")
    if isinstance(specific_data, dict):
        nested_call, nested_results, residual = _split_tool_specific_data(specific_data)
        if nested_call:
            call_data["toolSpecificData"] = nested_call
        if nested_results:
            result_data["toolSpecificData"] = nested_results
        if residual:
            event_data["toolSpecificData"] = residual
        else:
            event_data.pop("toolSpecificData")

    if call_data:
        label = _part_text(call_data.get("toolName")) or _part_text(
            call_data.get("invocationMessage")
        )
        result.append(
            ContentPart(
                "tool_call",
                json.dumps(call_data, ensure_ascii=False, indent=2, default=str),
                label=label or "Tool call/input",
            )
        )
    if result_data:
        result.append(_serialized_part("tool_result", "toolInvocationSerialized", result_data))
    if event_data or not (call_data or result_data):
        remainder = {"kind": "toolInvocationSerialized", **event_data}
        result.append(_serialized_part("tool_event", "toolInvocationSerialized", remainder))
    return result


def _split_tool_specific_data(
    value: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    call_keys = {"input", "arguments", "params", "command", "code"}
    result_keys = {
        "result",
        "output",
        "resulttext",
        "toolresult",
        "pasttensemessage",
        "resultdetails",
    }
    calls: Dict[str, Any] = {}
    results: Dict[str, Any] = {}
    remainder: Dict[str, Any] = {}
    for key, item in value.items():
        normalized = key.lower()
        if normalized in call_keys:
            calls[key] = item
        elif normalized in result_keys:
            results[key] = item
        else:
            remainder[key] = item
    return calls, results, remainder


def extract_response_text(response: Any) -> str:
    """Backward-compatible rendering used by callers outside the filter layer."""

    return render_message(extract_response_parts(response), "assistant")


def _request_timestamp(request: Dict[str, Any]) -> Optional[float]:
    for key in ("timestamp", "time", "createdAt", "created_at", "date"):
        epoch = to_epoch(request.get(key))
        if epoch is not None:
            return epoch
    return None


def _response_timestamp(request: Dict[str, Any], fallback: Optional[float]) -> float:
    model_state = request.get("modelState")
    if isinstance(model_state, dict):
        epoch = to_epoch(model_state.get("completedAt"))
        if epoch is not None:
            return epoch
    result = request.get("result")
    if isinstance(result, dict):
        timings = result.get("timings")
        if isinstance(timings, dict):
            elapsed = timings.get("totalElapsed")
            if isinstance(elapsed, (int, float)) and fallback is not None:
                return fallback + float(elapsed) / 1e3
    return fallback or 0.0


def _agent_id(request: Dict[str, Any]) -> Optional[str]:
    agent = request.get("agent")
    if not isinstance(agent, dict):
        return None
    extension = agent.get("extensionId")
    if isinstance(extension, dict):
        value = extension.get("value")
        if isinstance(value, str) and value:
            return value
    for key in ("fullName", "name", "id"):
        value = agent.get(key)
        if isinstance(value, str) and value.startswith("GitHub."):
            return value
    return None


def _reported_usage(request: Dict[str, Any], turn: int) -> Optional[UsageRecord]:
    result = request.get("result")
    result = result if isinstance(result, dict) else {}
    result_metadata = result.get("metadata")
    result_metadata = result_metadata if isinstance(result_metadata, dict) else {}
    summary = result_metadata.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    usage = summary.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    metadata = request.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    reported: Dict[str, Any] = {}
    if usage:
        reported["result.metadata.summary.usage"] = usage
    for path, container, key in (
        ("result.metadata.promptTokens", result_metadata, "promptTokens"),
        ("result.metadata.outputTokens", result_metadata, "outputTokens"),
        ("metadata.promptTokens", metadata, "promptTokens"),
        ("metadata.outputTokens", metadata, "outputTokens"),
    ):
        if key in container and container[key] is not None:
            reported[path] = container[key]
    thinking = summary.get("thinking")
    if isinstance(thinking, dict) and thinking.get("tokens") is not None:
        reported["result.metadata.summary.thinking.tokens"] = thinking["tokens"]
    tool_rounds = metadata.get("toolCallRounds")
    if isinstance(tool_rounds, dict):
        thinking = tool_rounds.get("thinking")
        if isinstance(thinking, dict) and thinking.get("tokens") is not None:
            reported["metadata.toolCallRounds.thinking.tokens"] = thinking["tokens"]
    if not reported:
        return None

    candidates = {
        "input_tokens": (
            ("result.metadata.summary.usage.prompt_tokens", usage.get("prompt_tokens")),
            ("result.metadata.summary.usage.input_tokens", usage.get("input_tokens")),
            ("result.metadata.promptTokens", result_metadata.get("promptTokens")),
            ("metadata.promptTokens", metadata.get("promptTokens")),
        ),
        "output_tokens": (
            ("result.metadata.summary.usage.completion_tokens", usage.get("completion_tokens")),
            ("result.metadata.summary.usage.output_tokens", usage.get("output_tokens")),
            ("result.metadata.outputTokens", result_metadata.get("outputTokens")),
            ("metadata.outputTokens", metadata.get("outputTokens")),
        ),
        "total_tokens": (
            ("result.metadata.summary.usage.total_tokens", usage.get("total_tokens")),
        ),
        "reasoning_tokens": (
            (
                "result.metadata.summary.usage.completion_tokens_details.reasoning_tokens",
                (
                    usage.get("completion_tokens_details", {}).get("reasoning_tokens")
                    if isinstance(usage.get("completion_tokens_details"), dict)
                    else None
                ),
            ),
            ("result.metadata.summary.usage.reasoning_tokens", usage.get("reasoning_tokens")),
        ),
        "cached_input_tokens": (
            (
                "result.metadata.summary.usage.prompt_tokens_details.cached_tokens",
                (
                    usage.get("prompt_tokens_details", {}).get("cached_tokens")
                    if isinstance(usage.get("prompt_tokens_details"), dict)
                    else None
                ),
            ),
        ),
    }
    fields: Dict[str, Dict[str, Any]] = {}
    for field_name, values in candidates.items():
        for source, value in values:
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                fields[field_name] = {"value": value, "source": source}
                break

    model = ""
    model_source = ""
    model_state = request.get("modelState")
    model_state = model_state if isinstance(model_state, dict) else {}
    for source, value in (
        ("request.modelId", request.get("modelId")),
        ("request.modelState.modelId", model_state.get("modelId")),
        ("request.modelState.selectedModelId", model_state.get("selectedModelId")),
    ):
        if isinstance(value, str) and value.strip():
            model = value.strip()
            model_source = source
            break
    return UsageRecord(turn, fields, reported, model, model_source)


def _normalise_messages(messages: List[Message]) -> List[Message]:
    result: List[Message] = []
    seen = set()
    for message in messages:
        if not message.content:
            continue
        key = (message.role, message.timestamp, message.content)
        if key in seen:
            continue
        seen.add(key)
        result.append(message)
    result.sort(key=lambda item: item.sort_key)
    return result


def session_from_state(
    state: Dict[str, Any],
    workspace: Workspace,
    fallback_id: str = "",
    fallback_ts: Optional[float] = None,
) -> Optional[Session]:
    if not isinstance(state, dict):
        return None

    requests = state.get("requests")
    messages: List[Message] = []
    agent = AGENT_DEFAULT
    timestamps: List[float] = []
    usage_records: List[UsageRecord] = []

    if isinstance(requests, list):
        turn = 0
        for request in requests:
            if not isinstance(request, dict):
                continue
            turn += 1
            usage_record = _reported_usage(request, turn)
            if usage_record is not None:
                usage_records.append(usage_record)
            request_ts = _request_timestamp(request)
            if request_ts is None:
                request_ts = fallback_ts
            user_text = extract_user_text(request)
            if user_text and request_ts is not None:
                messages.append(
                    Message(
                        "user",
                        user_text,
                        epoch_to_iso(request_ts),
                        request_ts,
                        parts=[ContentPart("prompt", user_text)],
                        turn=turn,
                    )
                )
                timestamps.append(request_ts)
            assistant_parts = extract_response_parts(request.get("response"))
            assistant_text = render_message(assistant_parts, "assistant")
            if assistant_text:
                response_ts = _response_timestamp(request, request_ts)
                turn_key = (request_ts if request_ts is not None else response_ts) + 0.000001
                messages.append(
                    Message(
                        "assistant",
                        assistant_text,
                        epoch_to_iso(response_ts),
                        turn_key,
                        parts=assistant_parts,
                        turn=turn,
                    )
                )
                timestamps.append(response_ts)
            detected_agent = _agent_id(request)
            if detected_agent:
                agent = detected_agent
    elif isinstance(state.get("messages"), list):
        for item in state["messages"]:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            if role not in ("user", "assistant"):
                continue
            content = _part_text(item.get("content") or item.get("text"))
            if not content:
                continue
            epoch = to_epoch(item.get("timestamp") or item.get("time")) or fallback_ts or 0.0
            kind = "prompt" if role == "user" else "answer"
            messages.append(
                Message(
                    role,
                    content.strip(),
                    epoch_to_iso(epoch),
                    epoch,
                    parts=[ContentPart(kind, content.strip())],
                )
            )
            timestamps.append(epoch)

    messages = _normalise_messages(messages)
    if not messages:
        return None

    session_id = (
        state.get("sessionId")
        or state.get("session_id")
        or state.get("id")
        or fallback_id
    )
    if not isinstance(session_id, str) or not session_id:
        session_id = "session-" + hashlib.sha1(
            json.dumps(state, sort_keys=True, default=str).encode("utf-8", "replace")
        ).hexdigest()[:16]

    created_ts = None
    for key in ("creationDate", "createdAt", "created_at", "created"):
        created_ts = to_epoch(state.get(key))
        if created_ts is not None:
            break
    if created_ts is None:
        created_ts = timestamps[0] if timestamps else (fallback_ts or 0.0)

    updated_ts = None
    for key in ("lastMessageDate", "updatedAt", "updated_at", "updated"):
        updated_ts = to_epoch(state.get(key))
        if updated_ts is not None:
            break
    if updated_ts is None:
        updated_ts = timestamps[-1] if timestamps else created_ts

    title = ""
    for key in ("customTitle", "title", "name"):
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            title = value.strip()
            break

    source = "sqlite" if fallback_id.startswith("db:") else "jsonl"
    return Session(
        session_id=session_id,
        workspace_id=workspace.workspace_id,
        title=title,
        agent=agent,
        created_ts=created_ts,
        updated_ts=updated_ts,
        messages=messages,
        source=source,
        usage=usage_records,
    )


def _set_path(root: Any, path: Any, value: Any, append: bool = False) -> None:
    if isinstance(path, str):
        try:
            path = json.loads(path)
        except ValueError:
            path = [path]
    if not isinstance(path, list) or not path:
        return

    current = root
    for raw_key in path[:-1]:
        key: Any = raw_key
        if isinstance(current, list):
            if not isinstance(key, int):
                try:
                    key = int(key)
                except (TypeError, ValueError):
                    return
            if key < 0 or key >= len(current):
                return
        elif isinstance(current, dict):
            key = str(key)
            if key not in current:
                return
        else:
            return
        current = current[key]

    final = path[-1]
    if isinstance(current, list):
        try:
            final = int(final)
        except (TypeError, ValueError):
            return
        if final < 0 or final > len(current):
            return
        if append and final < len(current) and isinstance(current[final], list):
            if isinstance(value, list):
                current[final].extend(value)
            else:
                current[final].append(value)
        elif final == len(current):
            current.append(value)
        else:
            current[final] = value
    elif isinstance(current, dict):
        key = str(final)
        if append and isinstance(current.get(key), list) and isinstance(value, list):
            current[key].extend(value)
        else:
            current[key] = value


def load_patched_state(path: Path) -> Optional[Dict[str, Any]]:
    """Replay the append-only patch log used by Copilot chat sessions."""

    state: Optional[Dict[str, Any]] = None
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            if kind == 0 and isinstance(entry.get("v"), dict):
                state = entry["v"]
                continue
            if state is None and isinstance(entry.get("v"), dict):
                candidate = entry["v"]
                if "sessionId" in candidate or "requests" in candidate:
                    state = candidate
                    continue
            if state is not None and "k" in entry:
                _set_path(state, entry.get("k"), entry.get("v"), append=(kind == 2))
    return state


def read_jsonl_sessions(path: Path, workspace: Workspace) -> List[Session]:
    state = load_patched_state(path)
    if state is None:
        return []
    try:
        fallback_ts = os.path.getmtime(path)
    except OSError:
        fallback_ts = None
    session = session_from_state(
        state,
        workspace,
        fallback_id=path.stem,
        fallback_ts=fallback_ts,
    )
    return [session] if session else []


def _parse_index(value: Any) -> Dict[str, SessionMeta]:
    result: Dict[str, SessionMeta] = {}
    entries: Any = value
    if isinstance(value, dict):
        entries = value.get("entries", value.get("sessions", value))

    if isinstance(entries, dict):
        iterator: Iterable[Tuple[Any, Any]] = entries.items()
    elif isinstance(entries, list):
        iterator = enumerate(entries)
    else:
        return result

    for key, item in iterator:
        if not isinstance(item, dict):
            continue
        session_id = item.get("sessionId") or item.get("session_id") or item.get("id") or key
        if not isinstance(session_id, str) or not session_id:
            continue
        timing = item.get("timing")
        timing = timing if isinstance(timing, dict) else {}
        created = (
            to_epoch(timing.get("created"))
            or to_epoch(item.get("createdAt"))
            or to_epoch(item.get("creationDate"))
        )
        updated = (
            to_epoch(item.get("lastMessageDate"))
            or to_epoch(timing.get("lastRequestEnded"))
            or to_epoch(timing.get("lastRequestStarted"))
            or to_epoch(item.get("updatedAt"))
        )
        title = item.get("title") or item.get("name") or item.get("customTitle") or ""
        result[session_id] = SessionMeta(
            session_id=session_id,
            title=title if isinstance(title, str) else "",
            created_ts=created,
            updated_ts=updated,
        )
    return result


def _parse_prompt_history(key: str, value: Any) -> Optional[PromptHistory]:
    history = value
    if isinstance(value, dict):
        history = value.get("history", value)
    prompts: List[str] = []

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            input_text = node.get("inputText")
            if isinstance(input_text, str) and input_text.strip():
                prompts.append(input_text.strip())
                return
            for child in node.values():
                if isinstance(child, (dict, list)):
                    collect(child)
        elif isinstance(node, list):
            for child in node:
                collect(child)

    collect(history)
    if not prompts:
        return None
    return PromptHistory(prompts=prompts, key=key)


def _looks_like_session(node: Dict[str, Any]) -> bool:
    if "requests" in node and isinstance(node.get("requests"), list):
        return True
    messages = node.get("messages")
    if isinstance(messages, list) and messages:
        return any(isinstance(item, dict) and item.get("role") in ("user", "assistant") for item in messages)
    return False


def _extract_generic_sessions(
    data: Any,
    workspace: Workspace,
    fallback_ts: Optional[float],
    found: List[Session],
    seen_ids: set,
    depth: int = 0,
) -> None:
    if depth > 8:
        return
    if isinstance(data, dict):
        if _looks_like_session(data):
            session = session_from_state(
                data,
                workspace,
                fallback_id="db:" + hashlib.sha1(
                    json.dumps(data, sort_keys=True, default=str).encode("utf-8", "replace")
                ).hexdigest()[:16],
                fallback_ts=fallback_ts,
            )
            if session and session.session_id not in seen_ids:
                seen_ids.add(session.session_id)
                found.append(session)
        for value in data.values():
            if isinstance(value, (dict, list)):
                _extract_generic_sessions(value, workspace, fallback_ts, found, seen_ids, depth + 1)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                _extract_generic_sessions(item, workspace, fallback_ts, found, seen_ids, depth + 1)


def read_db_rows(
    rows: List[Tuple[str, str]],
    workspace: Workspace,
    fallback_ts: Optional[float] = None,
) -> DbExtraction:
    extraction = DbExtraction()
    seen_ids: set = set()

    for key, raw in rows:
        try:
            value = json.loads(raw)
        except ValueError:
            continue

        if key == "chat.ChatSessionStore.index":
            extraction.index.update(_parse_index(value))
            continue

        if key.startswith("memento/interactive-session"):
            history = _parse_prompt_history(key, value)
            if history:
                extraction.prompt_histories.append(history)
            continue

        _extract_generic_sessions(value, workspace, fallback_ts, extraction.sessions, seen_ids)

    return extraction


def session_from_prompt_history(
    history: PromptHistory,
    workspace: Workspace,
    meta: Optional[SessionMeta] = None,
    fallback_ts: Optional[float] = None,
) -> Optional[Session]:
    if not history.prompts:
        return None

    created_ts = None
    if meta is not None:
        created_ts = meta.created_ts
    if created_ts is None:
        created_ts = history.created_ts or fallback_ts or 0.0

    messages: List[Message] = []
    for index, prompt in enumerate(history.prompts):
        epoch = created_ts + index * 0.001
        messages.append(
            Message(
                "user",
                prompt,
                epoch_to_iso(epoch),
                epoch,
                parts=[ContentPart("prompt", prompt)],
            )
        )

    digest = hashlib.sha1(
        "\x00".join(history.prompts).encode("utf-8", "replace")
    ).hexdigest()[:12]
    session_id = meta.session_id if meta else f"db-{workspace.workspace_id}-{digest}"
    title = ""
    if meta is not None:
        title = meta.title
    if not title or title.lower() in {"new chat", "untitled"}:
        title = guess_title(messages)

    updated_ts = max((message.sort_key for message in messages), default=created_ts)
    return Session(
        session_id=session_id,
        workspace_id=workspace.workspace_id,
        title=title,
        agent=AGENT_DEFAULT,
        created_ts=created_ts,
        updated_ts=updated_ts,
        messages=messages,
        source="sqlite",
    )


def _decode_agent_metadata(value: Any) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _agent_part_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, list):
        return "\n".join(filter(None, (_agent_part_text(item) for item in value)))
    if isinstance(value, dict):
        for key in ("content", "text", "value", "markdown", "output"):
            text = _agent_part_text(value.get(key))
            if text:
                return text
    return ""


def _agent_output_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def read_agent_session(
    path: Path,
    workspace: Workspace,
    fallback_ts: Optional[float] = None,
) -> Optional[AgentSessionExtraction]:
    """Read assistant parts and related tool outputs from an agent ``session.db``."""

    rows = read_agent_session_rows(path)
    if not rows.get("turns"):
        return None

    metadata = {
        str(key): _decode_agent_metadata(value)
        for key, value in rows.get("session_metadata", [])
    }
    session_id = path.parent.name
    title = metadata.get("customTitle") or metadata.get("title") or ""
    if not isinstance(title, str):
        title = ""
    working_directory = metadata.get("copilot.workingDirectory")
    if not isinstance(working_directory, str):
        working_directory = ""

    turns = [str(row[0]) for row in rows["turns"]]
    turn_numbers = {turn_id: index for index, turn_id in enumerate(turns, start=1)}
    output_records: List[Tuple[str, str, str, str]] = []
    for tool_call_id, _turn_id, output in rows.get("terminal_outputs", []):
        text = _agent_output_text(output)
        if text.strip():
            output_records.append(
                (str(tool_call_id), str(_turn_id), "terminal output", text)
            )
    for tool_call_id, _turn_id, kind, name, mime_type, content in rows.get(
        "tool_result_resources", []
    ):
        text = _agent_output_text(content)
        if text.strip():
            label = str(name or kind or mime_type or "tool result")
            output_records.append((str(tool_call_id), str(_turn_id), label, text))
    outputs_by_id: Dict[str, List[int]] = {}
    for index, (tool_call_id, _turn_id, _label, _text) in enumerate(output_records):
        outputs_by_id.setdefault(tool_call_id, []).append(index)

    parts_by_turn: Dict[str, List[ContentPart]] = {turn_id: [] for turn_id in turns}
    seen_parts: Dict[str, set] = {turn_id: set() for turn_id in turns}
    used_outputs: set[int] = set()
    for turn_id, raw_parts in rows.get("assistant_response_parts", []):
        turn_id = str(turn_id)
        if turn_id not in parts_by_turn:
            continue
        try:
            decoded_parts = json.loads(raw_parts)
        except (TypeError, ValueError):
            continue
        if isinstance(decoded_parts, dict):
            decoded_parts = [decoded_parts]
        if not isinstance(decoded_parts, list):
            continue
        for item in decoded_parts:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            part_id = item.get("id")
            if isinstance(part_id, str) and part_id:
                identity = (kind, part_id)
            else:
                identity = (kind, json.dumps(item, ensure_ascii=False, sort_keys=True, default=str))
            if identity in seen_parts[turn_id]:
                continue
            seen_parts[turn_id].add(identity)

            if kind == "markdown":
                text = _agent_part_text(item.get("content"))
                if text.strip():
                    parts_by_turn[turn_id].append(ContentPart("answer", text))
            elif kind == "reasoning":
                text = _agent_part_text(item.get("content"))
                if text.strip():
                    parts_by_turn[turn_id].append(ContentPart("reasoning", text))
            elif kind == "toolCall":
                tool_call_id = item.get("toolCallId")
                if not isinstance(tool_call_id, str) or not tool_call_id:
                    continue
                call_payload = {
                    key: value
                    for key, value in item.items()
                    if key not in {"kind", "id"}
                }
                call_data = json.dumps(call_payload, ensure_ascii=False, sort_keys=True)
                tool_name = item.get("toolName") or item.get("name")
                parts_by_turn[turn_id].append(
                    ContentPart(
                        "tool_call",
                        call_data,
                        label=(
                            str(tool_name)
                            if isinstance(tool_name, str) and tool_name.strip()
                            else "Copilot tool call"
                        ),
                    )
                )
                for output_index in outputs_by_id.get(tool_call_id, []):
                    if output_index not in used_outputs:
                        _, _, label, text = output_records[output_index]
                        parts_by_turn[turn_id].append(
                            ContentPart("tool_result", text, label=label)
                        )
                        used_outputs.add(output_index)
            elif kind:
                serialized = json.dumps(item, ensure_ascii=False, indent=2, default=str)
                parts_by_turn[turn_id].append(
                    ContentPart("tool_event", serialized, label=str(kind))
                )

    for index, (_tool_call_id, turn_id, label, text) in enumerate(output_records):
        if index not in used_outputs and turn_id in parts_by_turn:
            parts_by_turn[turn_id].append(ContentPart("tool_result", text, label=label))

    created_ts = fallback_ts or 0.0
    messages: List[Message] = []
    for turn_id in turns:
        number = turn_numbers[turn_id]
        parts = parts_by_turn[turn_id]
        if not parts:
            continue
        content = render_message(parts, "assistant")
        if not content:
            continue
        timestamp = created_ts + (number - 1) * 2 + 0.5
        messages.append(
            Message(
                "assistant",
                content,
                epoch_to_iso(timestamp),
                timestamp,
                parts=parts,
                turn=number,
            )
        )
    if not messages:
        return None

    session = Session(
        session_id=session_id,
        workspace_id=workspace.workspace_id,
        title=title,
        agent=AGENT_DEFAULT,
        created_ts=created_ts,
        updated_ts=max((message.sort_key for message in messages), default=created_ts),
        messages=messages,
        source="agentSessionData",
    )
    if not session.title:
        session.title = guess_title(session.messages, fallback="untitled")
    return AgentSessionExtraction(session, working_directory, len(turns))
