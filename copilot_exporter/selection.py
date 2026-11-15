"""Session-level selection shared by exports and read-only listings."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from .models import Session, Workspace, epoch_to_iso, to_epoch

ORDER_CHOICES = (
    "created-asc",
    "created-desc",
    "updated-asc",
    "updated-desc",
)
DEFAULT_ORDER = "updated-desc"


@dataclass(frozen=True)
class SessionSelection:
    sessions: Optional[frozenset[str]] = None
    last: Optional[int] = None
    since: Optional[float] = None
    filter_text: str = ""
    order: str = DEFAULT_ORDER

    def is_active(self) -> bool:
        return any(
            (
                self.sessions,
                self.last is not None,
                self.since is not None,
                self.filter_text.strip(),
            )
        )

    def as_dict(self) -> Dict[str, object]:
        return {
            "sessions": sorted(self.sessions) if self.sessions else None,
            "last": self.last,
            "since": epoch_to_iso(self.since) if self.since is not None else None,
            "filter": self.filter_text.strip() or None,
            "order": self.order,
        }

    def key(self) -> str:
        payload = json.dumps(
            self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def no_data_reason(self) -> str:
        return (
            "selection_matched_no_sessions"
            if self.is_active()
            else "no_sessions_found"
        )


def parse_session_ids(value: Optional[str]) -> Optional[frozenset[str]]:
    if not value:
        return None
    identifiers = {item.strip() for item in value.split(",") if item.strip()}
    return frozenset(identifiers) if identifiers else None


def parse_since(value: Optional[str]) -> Optional[float]:
    if value is None or not value.strip():
        return None
    epoch = to_epoch(value.strip())
    if epoch is None:
        raise ValueError(f"invalid --since value: {value}")
    return epoch


def _workspace_text(session: Session, workspaces: Dict[str, Workspace]) -> str:
    workspace = workspaces.get(session.workspace_id)
    if workspace is None:
        return session.workspace_id
    return workspace.path or workspace.workspace_id


def select_sessions(
    sessions: Iterable[Session],
    selection: SessionSelection,
    workspaces: Optional[Dict[str, Workspace]] = None,
) -> List[Session]:
    workspaces = workspaces or {}
    candidates = list(sessions)

    if selection.sessions:
        wanted = {value.lower() for value in selection.sessions}
        candidates = [
            session for session in candidates if session.session_id.lower() in wanted
        ]
    if selection.since is not None:
        candidates = [
            session for session in candidates if session.updated_ts >= selection.since
        ]
    if selection.filter_text.strip():
        needle = selection.filter_text.strip().lower()
        candidates = [
            session
            for session in candidates
            if needle
            in " ".join(
                (
                    session.title or "",
                    session.session_id,
                    session.workspace_id,
                    _workspace_text(session, workspaces),
                    session.source_file,
                )
            ).lower()
        ]

    axis, direction = selection.order.split("-", 1)

    def sort_key(session: Session):
        primary = session.created_ts if axis == "created" else session.updated_ts
        return (primary, session.created_ts, session.session_id)

    ordered = sorted(candidates, key=sort_key, reverse=(direction == "desc"))
    if selection.last is not None:
        ordered = ordered[: selection.last]
    return ordered


def chronological(sessions: Iterable[Session]) -> List[Session]:
    return sorted(
        sessions,
        key=lambda session: (
            session.created_ts,
            session.updated_ts,
            session.session_id,
        ),
    )
