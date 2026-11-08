"""Environment detection and source discovery for VS Code chat storage."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import unquote

from .models import Workspace
from .progress import Progress

CHAT_DIR_NAMES = {
    "chatSessions",
    "emptyWindowChatSessions",
    "transferredChatSessions",
}

_DB_NAMES = {"state.vscdb"}
_SESSION_DB_NAME = "session.db"
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@dataclass
class DbSource:
    path: Path
    workspace: "Workspace"


@dataclass
class JsonlSource:
    path: Path
    workspace: "Workspace"


@dataclass
class AgentSessionSource:
    path: Path
    workspace: "Workspace"


@dataclass
class SourceSet:
    dbs: List[DbSource] = field(default_factory=list)
    jsonls: List[JsonlSource] = field(default_factory=list)
    agent_sessions: List[AgentSessionSource] = field(default_factory=list)
    workspaces: Dict[str, "Workspace"] = field(default_factory=dict)
    env: str = "linux"

    def register(self, workspace: "Workspace") -> "Workspace":
        existing = self.workspaces.get(workspace.workspace_id)
        if existing is not None:
            if existing.path in ("", "global") and workspace.path not in ("", "global"):
                existing.path = workspace.path
            return existing
        self.workspaces[workspace.workspace_id] = workspace
        return workspace

    def workspace_label(self, workspace_id: str) -> str:
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            return workspace_id
        return workspace.path or workspace.workspace_id

    @property
    def is_empty(self) -> bool:
        return not self.dbs and not self.jsonls and not self.agent_sessions


class DiscoveryError(Exception):
    """Raised when no usable VS Code source can be found."""


def detect_env(path: Path) -> str:
    text = str(path).replace("\\", "/").lower()
    if "code-server" in text:
        return "code-server"
    if ".vscode-server" in text:
        return "vscode-server"
    if "library/application support" in text:
        return "macos"
    if os.name == "nt" or re.match(r"^[a-z]:/", text) or "/appdata/" in text:
        return "windows"
    return "linux"


def default_user_dirs() -> List[Path]:
    """Return known VS Code-family ``User`` directories for this machine."""

    home = Path.home()
    candidates: List[Path] = []

    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        localappdata = os.environ.get("LOCALAPPDATA")
        if appdata:
            appdata_path = Path(appdata)
            for name in ("Code", "Code - Insiders", "VSCodium", "Cursor", "Windsurf"):
                candidates.append(appdata_path / name / "User")
        if localappdata:
            candidates.append(Path(localappdata) / "Programs" / "Microsoft VS Code" / "data" / "user-data" / "User")
    else:
        for name in (
            "Code",
            "Code - Insiders",
            "VSCodium",
            "Cursor",
            "Code - OSS",
            "Windsurf",
        ):
            candidates.append(home / ".config" / name / "User")
            candidates.append(home / "Library" / "Application Support" / name / "User")
        candidates.append(home / ".config" / "code-server" / "User")
        candidates.append(home / ".local" / "share" / "code-server" / "User")
        candidates.append(home / ".vscode-server" / "data" / "User")

    seen: Set[str] = set()
    result: List[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_dir():
            result.append(candidate)
    return result


def _decode_file_uri(uri: str) -> Optional[str]:
    if not isinstance(uri, str):
        return None
    if uri.startswith("file://"):
        decoded = unquote(uri[7:])
        if re.match(r"^/[a-zA-Z]:/", decoded):
            decoded = decoded[1:]
        return decoded
    return None


def _read_workspace_path(workspace_dir: Path) -> Optional[str]:
    manifest = workspace_dir / "workspace.json"
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("folder", "workspace", "workspaceFolder"):
        decoded = _decode_file_uri(data.get(key, ""))
        if decoded:
            return decoded
    return None


def workspace_for_path(path: Path, fallback_root: Optional[Path] = None) -> "Workspace":
    """Infer a workspace id/path from a VS Code storage location."""

    resolved = path.resolve()
    parts = resolved.parts
    env = detect_env(resolved)

    if "workspaceStorage" in parts:
        index = parts.index("workspaceStorage")
        if index + 1 < len(parts):
            workspace_id = parts[index + 1]
            workspace_dir = Path(*parts[: index + 2])
            workspace_path = _read_workspace_path(workspace_dir) or workspace_id
            return Workspace(workspace_id, workspace_path, env)

    if "globalStorage" in parts or any(part in CHAT_DIR_NAMES for part in parts):
        if "globalStorage" in parts or "emptyWindowChatSessions" in parts or "transferredChatSessions" in parts:
            return Workspace("global", "global", env)

    anchor = fallback_root or (resolved if resolved.is_dir() else resolved.parent)
    digest = hashlib.sha1(str(anchor).encode("utf-8", "replace")).hexdigest()[:12]
    workspace_path = str(anchor)
    manifest_path = _read_workspace_path(anchor)
    if manifest_path:
        workspace_path = manifest_path
    return Workspace(f"custom-{digest}", workspace_path, env)


def _iter_chat_jsonl(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in {".git", "node_modules", "__pycache__"}]
        current = Path(dirpath)
        if current.name not in CHAT_DIR_NAMES:
            continue
        for filename in filenames:
            if filename.endswith(".jsonl"):
                yield current / filename


def _iter_state_dbs(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in {".git", "node_modules", "__pycache__"}]
        for filename in filenames:
            if filename in _DB_NAMES:
                yield Path(dirpath) / filename


def _iter_known_sources(root: Path) -> Iterable[Path]:
    """Yield storage files from the two directories VS Code actually uses."""

    for storage_name in ("globalStorage", "workspaceStorage"):
        storage = root / storage_name
        if not storage.is_dir():
            continue
        yield from storage.glob("state.vscdb")
        yield from storage.glob("*/state.vscdb")
        for chat_name in CHAT_DIR_NAMES:
            yield from storage.glob(f"{chat_name}/*.jsonl")
            yield from storage.glob(f"*/{chat_name}/*.jsonl")


def _add_db(
    sources: SourceSet,
    path: Path,
    fallback_root: Optional[Path] = None,
    seen: Set[str] = None,
) -> None:
    try:
        key = str(path.resolve())
    except OSError:
        key = str(path)
    seen = seen if seen is not None else set()
    if key in seen or not path.is_file():
        return
    seen.add(key)
    workspace = sources.register(workspace_for_path(path, fallback_root))
    sources.dbs.append(DbSource(path, workspace))


def _add_jsonl(
    sources: SourceSet,
    path: Path,
    fallback_root: Optional[Path] = None,
    seen: Set[str] = None,
) -> None:
    try:
        key = str(path.resolve())
    except OSError:
        key = str(path)
    seen = seen if seen is not None else set()
    if key in seen or not path.is_file():
        return
    seen.add(key)
    workspace = sources.register(workspace_for_path(path, fallback_root))
    sources.jsonls.append(JsonlSource(path, workspace))


def _add_agent_session(
    sources: SourceSet,
    path: Path,
    fallback_root: Optional[Path] = None,
    seen: Set[str] = None,
) -> None:
    try:
        key = str(path.resolve())
    except OSError:
        key = str(path)
    seen = seen if seen is not None else set()
    if key in seen or not path.is_file():
        return
    if path.name != _SESSION_DB_NAME or not _UUID_RE.match(path.parent.name):
        return
    seen.add(key)
    workspace = sources.register(workspace_for_path(path, fallback_root))
    sources.agent_sessions.append(AgentSessionSource(path, workspace))


def _agent_session_root(root: Path) -> Optional[Path]:
    """Find the agent-session store adjacent to a VS Code User directory."""

    resolved = root.resolve()
    if resolved.name == "agentSessionData":
        return resolved
    if (resolved / "agentSessionData").is_dir():
        return resolved / "agentSessionData"
    for parent in (resolved, *resolved.parents):
        if parent.name == "User":
            candidate = parent.parent / "agentSessionData"
            return candidate if candidate.is_dir() else None
    return None


def _iter_agent_session_dbs(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return
    for path in root.glob("*/session.db"):
        if _UUID_RE.match(path.parent.name):
            yield path


def collect_from_root(
    sources: SourceSet,
    root: Path,
    progress: Optional[Progress] = None,
    known_only: bool = False,
) -> None:
    """Collect DBs and chat JSONL files below an arbitrary directory."""

    seen_dbs: Set[str] = set()
    seen_jsonls: Set[str] = set()
    seen_agent_sessions: Set[str] = set()
    if known_only:
        for path in _iter_known_sources(root):
            if path.name in _DB_NAMES:
                _add_db(sources, path, root, seen_dbs)
            elif path.suffix.lower() == ".jsonl":
                _add_jsonl(sources, path, root, seen_jsonls)
            if progress is not None:
                progress.step(str(path))
        agent_root = _agent_session_root(root)
        if agent_root is not None:
            for path in _iter_agent_session_dbs(agent_root):
                _add_agent_session(sources, path, root, seen_agent_sessions)
                if progress is not None:
                    progress.step(str(path))
        return
    for db_path in _iter_state_dbs(root):
        _add_db(sources, db_path, root, seen_dbs)
        if progress is not None:
            progress.step(str(db_path))
    for jsonl_path in _iter_chat_jsonl(root):
        _add_jsonl(sources, jsonl_path, root, seen_jsonls)
        if progress is not None:
            progress.step(str(jsonl_path))
    agent_root = _agent_session_root(root)
    if agent_root is not None:
        for path in _iter_agent_session_dbs(agent_root):
            _add_agent_session(sources, path, root, seen_agent_sessions)
            if progress is not None:
                progress.step(str(path))


def discover(
    source: Optional[str] = None,
    db_path: Optional[str] = None,
    progress: Optional[Progress] = None,
) -> SourceSet:
    """Discover chat sources using an explicit path or environment defaults."""

    sources = SourceSet()
    seen_dbs: Set[str] = set()
    seen_jsonls: Set[str] = set()
    if progress is not None:
        progress.start("Scanning sources")

    if db_path:
        path = Path(db_path).expanduser()
        if not path.is_file():
            raise DiscoveryError(f"database not found: {path}")
        _add_db(sources, path, path.parent, seen_dbs)
        collect_from_root(sources, path.parent, progress=progress)

    if source:
        path = Path(source).expanduser()
        if not path.exists():
            raise DiscoveryError(f"source not found: {path}")
        if path.is_file():
            suffix = path.suffix.lower()
            if suffix == ".jsonl":
                _add_jsonl(sources, path, path.parent, seen_jsonls)
            elif path.name == _SESSION_DB_NAME and _UUID_RE.match(path.parent.name):
                _add_agent_session(sources, path, path.parent.parent, set())
            else:
                _add_db(sources, path, path.parent, seen_dbs)
                collect_from_root(sources, path.parent, progress=progress)
        else:
            is_user_dir = (path / "globalStorage").is_dir() or (
                path / "workspaceStorage"
            ).is_dir()
            collect_from_root(
                sources, path, progress=progress, known_only=is_user_dir
            )
            if not sources.dbs and not sources.jsonls:
                for jsonl_path in path.glob("*.jsonl"):
                    _add_jsonl(sources, jsonl_path, path, seen_jsonls)
    elif not db_path:
        user_dirs = default_user_dirs()
        for user_dir in user_dirs:
            collect_from_root(sources, user_dir, progress=progress, known_only=True)

    if progress is not None:
        progress.done()

    if sources.is_empty:
        locations = ", ".join(str(path) for path in default_user_dirs()) or "none"
        raise DiscoveryError(
            "no VS Code chat storage was found. Pass --source/--db explicitly. "
            f"Checked: {locations}"
        )

    envs = {workspace.env for workspace in sources.workspaces.values()}
    if len(envs) == 1:
        sources.env = next(iter(envs))
    elif envs:
        sources.env = "mixed"
    else:
        sources.env = "unknown"
    return sources
