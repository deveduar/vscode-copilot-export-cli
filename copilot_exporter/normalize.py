"""Merge, enrich and chronologically normalize discovered sessions."""

from __future__ import annotations

import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import unquote

from .db import DatabaseError, read_chat_rows
from .discovery import SourceSet, workspace_for_path
from .models import ContentPart, Message, Session, Workspace, epoch_to_iso, guess_title
from .progress import Progress
from .readers import (
    DbExtraction,
    PromptHistory,
    SessionMeta,
    read_agent_session,
    read_db_rows,
    read_jsonl_sessions,
    session_from_prompt_history,
)


@dataclass
class Assembled:
    sessions: List[Session] = field(default_factory=list)
    workspaces: Dict[str, Workspace] = field(default_factory=dict)


def _is_generic_title(title: str) -> bool:
    return not title or title.strip().lower() in {"new chat", "untitled", "chat"}


def _merge_messages(left: List[Message], right: List[Message]) -> List[Message]:
    seen = set()
    merged: List[Message] = []
    for message in left + right:
        if not message.content:
            continue
        key = (message.role, message.timestamp, message.content)
        if key in seen:
            continue
        seen.add(key)
        merged.append(message)
    merged.sort(key=lambda item: item.sort_key)
    return merged


def _merge_session(target: Session, incoming: Session) -> None:
    target.messages = _merge_messages(target.messages, incoming.messages)
    usage_by_turn = {record.turn: record for record in target.usage}
    for record in incoming.usage:
        existing = usage_by_turn.get(record.turn)
        if existing is None:
            target.usage.append(record)
            usage_by_turn[record.turn] = record
            continue
        for name, value in record.fields.items():
            existing.fields.setdefault(name, value)
        for name, value in record.reported.items():
            existing.reported.setdefault(name, value)
        if not existing.model and record.model:
            existing.model = record.model
            existing.model_source = record.model_source
    target.created_ts = min(target.created_ts, incoming.created_ts)
    target.updated_ts = max(target.updated_ts, incoming.updated_ts)
    if _is_generic_title(target.title) and not _is_generic_title(incoming.title):
        target.title = incoming.title
    if incoming.agent and incoming.agent != "GitHub.copilot-chat":
        target.agent = incoming.agent


def _apply_meta(session: Session, meta: SessionMeta) -> None:
    if _is_generic_title(session.title) and not _is_generic_title(meta.title):
        session.title = meta.title
    if meta.created_ts is not None and session.created_ts <= 0:
        session.created_ts = meta.created_ts
    if meta.updated_ts is not None and meta.updated_ts > session.updated_ts:
        session.updated_ts = meta.updated_ts
    if _is_generic_title(session.title):
        session.title = guess_title(session.messages, fallback=session.title or "untitled")


def _normalise_path(value: str) -> str:
    if value.startswith("file://"):
        value = unquote(value[7:])
        if len(value) >= 3 and value[0] == "/" and value[2] == ":":
            value = value[1:]
    return os.path.normcase(os.path.normpath(os.path.expanduser(value)))


def _workspace_for_agent_session(
    working_directory: str,
    fallback: Workspace,
    workspaces: Dict[str, Workspace],
) -> Workspace:
    if working_directory:
        target = _normalise_path(working_directory)
        for workspace in workspaces.values():
            if workspace.path and _normalise_path(workspace.path) == target:
                return workspace
        decoded_path = target
        inferred = workspace_for_path(
            Path(decoded_path),
            fallback_root=Path(decoded_path),
        )
        return workspaces.setdefault(inferred.workspace_id, inferred)
    return fallback


def _attach_prompt_history(session: Session, history: PromptHistory) -> None:
    if not history.prompts:
        return
    base = session.created_ts
    assistant_turns = {
        message.turn
        for message in session.messages
        if message.role == "assistant" and message.turn > 0
    }
    prompts = [
        Message(
            "user",
            prompt,
            timestamp=epoch_to_iso(base + (index - 1) * 2),
            sort_key=base + (index - 1) * 2,
            parts=[ContentPart("prompt", prompt)],
            turn=index,
        )
        for index, prompt in enumerate(history.prompts, start=1)
    ]
    for message in session.messages:
        if message.role == "assistant" and message.turn > 0:
            message.sort_key = base + (message.turn - 1) * 2 + 0.5
            message.timestamp = epoch_to_iso(message.sort_key)
    session.messages = _merge_messages(prompts, session.messages)
    if _is_generic_title(session.title):
        session.title = guess_title(session.messages, fallback=session.title or "untitled")
    if assistant_turns:
        session.updated_ts = max(
            session.updated_ts,
            base + (max(assistant_turns) - 1) * 2 + 0.5,
        )


def assemble(
    sources: SourceSet,
    progress: Optional[Progress] = None,
) -> Assembled:
    merged: Dict[str, Session] = {}
    workspace_sessions: Dict[str, List[Session]] = defaultdict(list)
    metas: Dict[str, Dict[str, SessionMeta]] = defaultdict(dict)
    prompt_histories: Dict[str, List[PromptHistory]] = defaultdict(list)
    fallback_times: Dict[str, float] = {}
    if progress is not None:
        progress.start(
            "Reading sources",
            total=len(sources.jsonls) + len(sources.dbs) + len(sources.agent_sessions),
        )

    def add(session: Optional[Session]) -> None:
        if session is None or not session.messages:
            return
        existing = merged.get(session.session_id)
        if existing is None:
            merged[session.session_id] = session
            workspace_sessions[session.workspace_id].append(session)
            return
        _merge_session(existing, session)

    for source in sources.jsonls:
        if progress is not None:
            progress.step(source.path.name)
        try:
            fallback = os.path.getmtime(source.path)
        except OSError:
            fallback = 0.0
        for session in read_jsonl_sessions(source.path, source.workspace):
            if not session.created_ts:
                session.created_ts = fallback
            if not session.updated_ts:
                session.updated_ts = fallback
            add(session)

    if sources.dbs:
        rows_by_source: List[object] = [None] * len(sources.dbs)
        workers = min(16, len(sources.dbs))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(read_chat_rows, source.path): index
                for index, source in enumerate(sources.dbs)
            }
            for future in as_completed(futures):
                index = futures[future]
                rows_by_source[index] = future.result()
                if progress is not None:
                    progress.step(sources.dbs[index].path.name)

    for index, source in enumerate(sources.dbs):
        try:
            fallback = os.path.getmtime(source.path)
        except OSError:
            fallback = 0.0
        fallback_times[source.workspace.workspace_id] = fallback
        extraction: DbExtraction = read_db_rows(
            rows_by_source[index], source.workspace, fallback
        )
        metas[source.workspace.workspace_id].update(extraction.index)
        for session in extraction.sessions:
            add(session)
        prompt_histories[source.workspace.workspace_id].extend(extraction.prompt_histories)

    agent_sessions_by_workspace = defaultdict(list)
    for source in sources.agent_sessions:
        if progress is not None:
            progress.step(source.path.name)
        try:
            fallback = os.path.getmtime(source.path)
        except OSError:
            fallback = 0.0
        extraction = read_agent_session(source.path, source.workspace, fallback)
        if extraction is None:
            continue
        workspace = _workspace_for_agent_session(
            extraction.working_directory,
            source.workspace,
            sources.workspaces,
        )
        extraction.session.workspace_id = workspace.workspace_id
        if workspace.workspace_id not in sources.workspaces:
            sources.register(workspace)
        agent_sessions_by_workspace[workspace.workspace_id].append(extraction)
        add(extraction.session)

    for workspace_id, agent_extractions in agent_sessions_by_workspace.items():
        histories = prompt_histories.get(workspace_id, [])
        unique_histories: List[PromptHistory] = []
        seen_prompts = set()
        for history in histories:
            fingerprint = tuple(history.prompts)
            if fingerprint not in seen_prompts:
                seen_prompts.add(fingerprint)
                unique_histories.append(history)

        unmatched_sessions = list(agent_extractions)
        unmatched_histories = list(unique_histories)
        for history in list(unmatched_histories):
            matching = [
                extraction
                for extraction in unmatched_sessions
                if extraction.turn_count == len(history.prompts)
            ]
            if len(matching) == 1:
                extraction = matching[0]
                _attach_prompt_history(extraction.session, history)
                unmatched_sessions.remove(extraction)
                unmatched_histories.remove(history)
        if len(unmatched_sessions) == 1 and len(unmatched_histories) == 1:
            _attach_prompt_history(
                unmatched_sessions[0].session, unmatched_histories[0]
            )

    for session in merged.values():
        meta = metas.get(session.workspace_id, {}).get(session.session_id)
        if meta is not None:
            _apply_meta(session, meta)

    for workspace_id, histories in prompt_histories.items():
        if workspace_sessions.get(workspace_id):
            continue
        workspace = sources.workspaces.get(workspace_id)
        if workspace is None:
            continue
        fallback = fallback_times.get(workspace_id, 0.0)
        for history in histories:
            session = session_from_prompt_history(history, workspace, fallback_ts=fallback)
            add(session)

    sessions = [session for session in merged.values() if session.messages]
    for index, session in enumerate(sessions):
        session.messages.sort(key=lambda item: item.sort_key)
        session.order = index
        if not session.created_ts:
            session.created_ts = session.messages[0].sort_key or 0.0
        if not session.updated_ts:
            session.updated_ts = session.messages[-1].sort_key or session.created_ts
        if _is_generic_title(session.title):
            session.title = guess_title(session.messages, fallback="untitled")

    sessions.sort(key=lambda item: (item.created_ts, item.updated_ts, item.session_id))
    if progress is not None:
        progress.done()
    return Assembled(sessions=sessions, workspaces=sources.workspaces)
