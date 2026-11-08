"""Core data model and time normalization helpers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?"
    r"(Z|[+-]\d{2}:?\d{2})?$"
)


def to_epoch(value: Any) -> Optional[float]:
    """Convert common VS Code timestamp representations to Unix seconds."""

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _numeric_epoch(float(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return _numeric_epoch(float(text))
        except ValueError:
            pass
        match = _ISO_RE.match(text)
        if not match:
            return None
        year, month, day, hour, minute, second, fraction, zone = match.groups()
        micro = 0
        if fraction:
            micro = int((fraction + "000000")[:6])
        tz = timezone.utc
        if zone and zone != "Z":
            sign = 1 if zone[0] == "+" else -1
            raw = zone[1:].replace(":", "")
            offset = int(raw[:2]) * 60 + int(raw[2:])
            from datetime import timedelta

            tz = timezone(sign * timedelta(minutes=offset))
        try:
            dt = datetime(
                int(year),
                int(month),
                int(day),
                int(hour or 0),
                int(minute or 0),
                int(second or 0),
                micro,
                tzinfo=tz,
            )
        except ValueError:
            return None
        return dt.timestamp()
    return None


def _numeric_epoch(number: float) -> Optional[float]:
    if number <= 0:
        return None
    if number >= 1e17:
        return number / 1e9
    if number >= 1e14:
        return number / 1e6
    if number >= 1e11:
        return number / 1e3
    if number >= 1e9:
        return number
    return number


def to_iso(value: Any) -> Optional[str]:
    """Return an ISO-8601 UTC timestamp or None."""

    epoch = to_epoch(value)
    if epoch is None:
        return None
    return epoch_to_iso(epoch)


def epoch_to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ContentPart:
    """A typed, independently exportable fragment of a chat message."""

    kind: str
    content: str
    label: str = ""
    truncated: bool = False
    original_chars: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "type": self.kind,
            "label": self.label,
            "content": self.content,
            "truncated": self.truncated,
            "original_chars": self.original_chars or len(self.content),
        }


@dataclass
class UsageRecord:
    """Usage explicitly reported by a stored Copilot request."""

    turn: int
    fields: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    reported: Dict[str, Any] = field(default_factory=dict)
    model: str = ""
    model_source: str = ""

    @property
    def completeness(self) -> str:
        expected = {"input_tokens", "output_tokens", "total_tokens"}
        return "complete" if expected.issubset(self.fields) else "partial"

    def as_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "turn": self.turn,
            "source": "reported",
            "completeness": self.completeness,
            "fields": self.fields,
            "reported": self.reported,
        }
        if self.model:
            result["model"] = self.model
            result["model_source"] = self.model_source
        return result


@dataclass
class Message:
    role: str
    content: str
    timestamp: Optional[str] = None
    sort_key: float = 0.0
    parts: List[ContentPart] = field(default_factory=list)
    turn: int = 0

    def message_id(self) -> str:
        raw = "\x00".join((self.role, self.timestamp or "", self.content))
        return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()

    def as_dict(self) -> Dict[str, Any]:
        parts = self.parts or (
            [ContentPart("prompt" if self.role == "user" else "answer", self.content)]
            if self.content
            else []
        )
        return {
            "role": self.role,
            "timestamp": self.timestamp,
            "turn": self.turn,
            "content": self.content,
            "parts": [part.as_dict() for part in parts],
        }


@dataclass
class Session:
    session_id: str
    workspace_id: str
    title: str
    agent: str
    created_ts: float
    updated_ts: float
    messages: List[Message] = field(default_factory=list)
    source: str = ""
    order: int = 0
    usage: List[UsageRecord] = field(default_factory=list)
    selected_turn_count: Optional[int] = field(default=None, repr=False)

    @property
    def created_at(self) -> str:
        return epoch_to_iso(self.created_ts)

    @property
    def updated_at(self) -> str:
        return epoch_to_iso(self.updated_ts)

    def as_dict(self, workspace: str) -> Dict[str, Any]:
        result = {
            "session_id": self.session_id,
            "workspace": workspace,
            "workspace_id": self.workspace_id,
            "title": self.title,
            "agent": self.agent,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [message.as_dict() for message in self.messages],
        }
        if self.usage:
            result["usage"] = [record.as_dict() for record in self.usage]
        return result

    def fingerprint(self, workspace: str) -> str:
        import json

        payload = json.dumps(
            self.as_dict(workspace),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def message_ids(self) -> List[str]:
        return [message.message_id() for message in self.messages]


@dataclass
class Workspace:
    workspace_id: str
    path: str
    env: str = "linux"

    @property
    def label(self) -> str:
        return self.path or self.workspace_id


def guess_title(messages: Iterable[Message], fallback: str = "untitled") -> str:
    for message in messages:
        if message.role != "user":
            continue
        text = " ".join(message.content.split())
        if text:
            return text[:80]
    return fallback
