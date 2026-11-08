from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PosixPath
from types import SimpleNamespace
from unittest.mock import patch

from copilot_exporter.cli import (
    EXIT_NO_DATA,
    EXIT_OK,
    EXIT_SOURCE_ERROR,
    build_parser,
    main,
)
from copilot_exporter.discovery import default_user_dirs, discover
from copilot_exporter.filters import FilterOptions, filter_sessions, render_message
from copilot_exporter.models import ContentPart, Message, Session, UsageRecord, to_epoch, to_iso
from copilot_exporter.normalize import assemble
from copilot_exporter.readers import (
    extract_response_parts,
    read_jsonl_sessions,
    session_from_state,
)
from copilot_exporter.writers import (
    _usage_summary,
    create_run_directory,
    split_markdown_session,
)
from copilot_exporter.discovery import workspace_for_path


def _request(index: int, text: str, answer: str, timestamp: int) -> dict:
    return {
        "requestId": f"request-{index}",
        "timestamp": timestamp,
        "message": {"text": text, "parts": [{"text": text, "kind": "text"}]},
        "response": [
            {"value": answer},
            {"kind": "thinking", "value": "private reasoning"},
            {
                "kind": "toolInvocationSerialized",
                "invocationMessage": {"value": "tool"},
                "result": {"value": "tool result " * 10},
            },
        ],
        "modelState": {"completedAt": timestamp + 1500},
        "agent": {"extensionId": {"value": "GitHub.copilot-chat"}},
    }


def write_jsonl(path: Path, session_id: str, requests: list[dict], title: str = "Test chat") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    first = {
        "kind": 0,
        "v": {
            "version": 3,
            "creationDate": requests[0]["timestamp"] - 1000,
            "customTitle": title,
            "sessionId": session_id,
            "requests": [requests[0]],
        },
    }
    lines = [first]
    for item in requests[1:]:
        lines.append({"kind": 2, "k": ["requests"], "v": [item]})
    with path.open("w", encoding="utf-8") as handle:
        for item in lines:
            handle.write(json.dumps(item) + "\n")


def create_item_table(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE itemTable (key TEXT PRIMARY KEY, value TEXT)")
    return connection


def create_agent_session(path: Path, working_directory: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE session_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE turns (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            checkpoint_ref TEXT
        );
        CREATE TABLE assistant_response_parts (
            response_id TEXT PRIMARY KEY,
            turn_id TEXT NOT NULL,
            parts TEXT NOT NULL
        );
        CREATE TABLE terminal_outputs (
            tool_call_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            output BLOB NOT NULL
        );
        CREATE TABLE tool_result_resources (
            tool_call_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            turn_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            name TEXT,
            mime_type TEXT,
            uri TEXT,
            size INTEGER,
            content BLOB
        );
        """
    )
    connection.executemany(
        "INSERT INTO session_metadata VALUES (?, ?)",
        [
            ("customTitle", "Agent-backed chat"),
            ("copilot.workingDirectory", working_directory),
        ],
    )
    turns = ["request-one", "request-two"]
    connection.executemany(
        "INSERT INTO turns VALUES (?, ?, NULL)",
        [(turn_id, f"event-{index}") for index, turn_id in enumerate(turns, 1)],
    )
    parts = [
        [
            {"id": "answer-one", "kind": "markdown", "content": "Agent answer one."},
            {
                "kind": "toolCall",
                "toolCallId": "terminal-one",
                "toolName": "terminal",
                "input": {"command": "echo hello"},
            },
        ],
        [{"id": "answer-two", "kind": "markdown", "content": "Agent answer two."}],
    ]
    connection.executemany(
        "INSERT INTO assistant_response_parts VALUES (?, ?, ?)",
        [
            (f"response-{index}", turn_id, json.dumps(items))
            for index, (turn_id, items) in enumerate(zip(turns, parts), 1)
        ],
    )
    connection.execute(
        "INSERT INTO terminal_outputs VALUES (?, ?, ?)",
        ("terminal-one", turns[0], b"terminal output one"),
    )
    connection.execute(
        "INSERT INTO terminal_outputs VALUES (?, ?, ?)",
        ("unreferenced-tool-call", turns[1], b"unreferenced terminal output"),
    )
    connection.execute(
        "INSERT INTO tool_result_resources VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
        (
            "resource-tool-call",
            1,
            turns[1],
            "text",
            "resource output",
            "text/plain",
            15,
            b"resource result",
        ),
    )
    connection.commit()
    connection.close()


def make_fixture(root: Path) -> dict:
    user = root / "User"
    global_storage = user / "globalStorage"
    global_storage.mkdir(parents=True)

    global_id = "aaaaaaaa-1111-2222-3333-444444444444"
    write_jsonl(
        global_storage / "emptyWindowChatSessions" / f"{global_id}.jsonl",
        global_id,
        [
            _request(
                1,
                "Global question",
                "Global answer with code:\n```python\nprint('hi')\n```",
                1700000001000,
            )
        ],
        title="Global chat",
    )

    workspace_id = "abcdef0123456789abcdef0123456789"
    workspace_dir = user / "workspaceStorage" / workspace_id
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "workspace.json").write_text(
        json.dumps({"folder": "file:///home/user/project"}),
        encoding="utf-8",
    )

    session_id = "bbbbbbbb-1111-2222-3333-444444444444"
    write_jsonl(
        workspace_dir / "chatSessions" / f"{session_id}.jsonl",
        session_id,
        [
            _request(1, "First question", "First answer", 1700000101000),
            _request(2, "Second question", "Second answer", 1700000201000),
        ],
        title="New Chat",
    )

    connection = create_item_table(workspace_dir / "state.vscdb")
    index = {
        "version": 1,
        "entries": {
            session_id: {
                "sessionId": session_id,
                "title": "Indexed project chat",
                "lastMessageDate": 1700000203000,
                "timing": {"created": 1700000100000},
            }
        },
    }
    connection.execute(
        "INSERT INTO itemTable VALUES (?, ?)",
        ("chat.ChatSessionStore.index", json.dumps(index)),
    )
    connection.commit()
    connection.close()

    db_only_id = "cccccccc-1111-2222-3333-444444444444"
    db_only_dir = user / "workspaceStorage" / "11112222333344445555666677778888"
    db_only_dir.mkdir(parents=True)
    (db_only_dir / "workspace.json").write_text(
        json.dumps({"folder": "file:///home/user/db-only"}),
        encoding="utf-8",
    )
    connection = create_item_table(db_only_dir / "state.vscdb")
    history = {
        "history": {
            "copilot": [
                {"inputText": "DB prompt one", "attachments": []},
                {"inputText": "DB prompt two", "attachments": []},
            ]
        }
    }
    connection.execute(
        "INSERT INTO itemTable VALUES (?, ?)",
        ("memento/interactive-session", json.dumps(history)),
    )
    connection.commit()
    connection.close()

    agent_session_id = "dddddddd-1111-2222-3333-444444444444"
    create_agent_session(
        root / "agentSessionData" / agent_session_id / "session.db",
        "file:///home/user/db-only",
    )

    return {
        "root": user,
        "workspace_id": workspace_id,
        "session_id": session_id,
        "global_id": global_id,
        "db_only_id": db_only_id,
        "agent_session_id": agent_session_id,
    }


def first_bundle(output: Path) -> Path:
    return sorted(path for path in output.iterdir() if path.is_dir())[0]


def latest_bundle(output: Path) -> Path:
    return sorted(path for path in output.iterdir() if path.is_dir())[-1]


class TimestampTests(unittest.TestCase):
    def test_milliseconds(self) -> None:
        self.assertEqual(to_iso(1700000000000), "2023-11-14T22:13:20Z")

    def test_seconds(self) -> None:
        self.assertEqual(to_iso(1700000000), "2023-11-14T22:13:20Z")

    def test_iso(self) -> None:
        self.assertEqual(to_iso("2023-11-14T22:13:20Z"), "2023-11-14T22:13:20Z")

    def test_invalid(self) -> None:
        self.assertIsNone(to_epoch("not a date"))
        self.assertIsNone(to_epoch(None))


class JsonlReaderTests(unittest.TestCase):
    def test_raw_reasoning_is_omitted_unless_requested(self) -> None:
        raw_reasoning = {
            "kind": "thinking",
            "value": "",
            "id": "opaque-id",
            "metadata": {"vscodeReasoningDone": True, "stopReason": "other"},
        }
        parts = extract_response_parts(
            [
                {"kind": "thinking", "value": "Readable reasoning"},
                raw_reasoning,
                {"value": "The answer."},
            ]
        )
        session = Session(
            "sid",
            "ws",
            "Reasoning",
            "agent",
            0.0,
            1.0,
            [Message("assistant", "", "2026-11-05T00:00:00Z", 1.0, parts=parts)],
        )

        filtered, stats = filter_sessions(
            [session],
            FilterOptions(content=frozenset({"reasoning"})),
        )
        rendered = filtered[0].messages[0].content
        self.assertIn("Readable reasoning", rendered)
        self.assertNotIn("opaque-id", rendered)
        self.assertIn("serialized_reasoning omitted", rendered)
        self.assertEqual(stats.excluded_parts["reasoning"], 1)
        self.assertEqual(
            stats.omissions[0]["categories"],
            ["serialized_reasoning"],
        )

        filtered, stats = filter_sessions(
            [session],
            FilterOptions(
                content=frozenset({"reasoning"}),
                include_raw_reasoning=True,
            ),
        )
        rendered = filtered[0].messages[0].content
        self.assertIn("opaque-id", rendered)
        self.assertNotIn("serialized_reasoning omitted", rendered)
        self.assertEqual(stats.emitted_parts["reasoning"], 2)

    def test_patch_append_and_response_parts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            requests = [
                _request(1, "One", "Answer one", 1700000001000),
                _request(2, "Two", "Answer two", 1700000002000),
            ]
            write_jsonl(path, "sid", requests, title="Chat")
            workspace = workspace_for_path(path)
            sessions = read_jsonl_sessions(path, workspace)

            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertEqual([m.role for m in session.messages], ["user", "assistant", "user", "assistant"])
            self.assertEqual([m.content for m in session.messages][::2], ["One", "Two"])
            assistant = session.messages[1].content
            self.assertIn("Answer one", assistant)
            self.assertIn("## Reasoning", assistant)
            self.assertIn("private reasoning", assistant)
            self.assertIn("### Tool call/input", assistant)
            self.assertIn("### Tool result", assistant)
            self.assertIn("tool result", assistant)
            self.assertNotIn('"invocationMessage"', assistant)

    def test_reported_usage_is_preserved_and_exported_without_part_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            first = _request(1, "One", "Answer one", 1700000001000)
            first["modelId"] = "example-model"
            first["result"] = {
                "metadata": {
                    "promptTokens": 999,
                    "outputTokens": 888,
                    "summary": {
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 4,
                            "total_tokens": 14,
                            "completion_tokens_details": {"reasoning_tokens": 2},
                            "prompt_tokens_details": {"cached_tokens": 1},
                        }
                    },
                }
            }
            second = _request(2, "Two", "Answer two", 1700000002000)
            second["result"] = {
                "metadata": {"promptTokens": 7, "outputTokens": 3}
            }
            write_jsonl(path, "usage-session", [first, second])

            session = read_jsonl_sessions(path, workspace_for_path(path))[0]
            self.assertEqual([record.turn for record in session.usage], [1, 2])
            self.assertEqual(session.usage[0].fields["input_tokens"]["value"], 10)
            self.assertEqual(
                session.usage[0].fields["input_tokens"]["source"],
                "result.metadata.summary.usage.prompt_tokens",
            )
            self.assertEqual(session.usage[0].fields["total_tokens"]["value"], 14)
            self.assertEqual(session.usage[0].fields["reasoning_tokens"]["value"], 2)
            self.assertEqual(session.usage[0].model, "example-model")
            self.assertEqual(session.usage[1].completeness, "partial")
            self.assertEqual(
                session.usage[0].reported["result.metadata.promptTokens"], 999
            )

            markdown_output = Path(tmp) / "markdown-output"
            self.assertEqual(
                main(
                    [
                        "--source",
                        str(path),
                        "--output",
                        str(markdown_output),
                        "--format",
                        "markdown",
                        "--format",
                        "json",
                        "--quiet",
                    ]
                ),
                EXIT_OK,
            )
            markdown_bundle = first_bundle(markdown_output)
            markdown = next(markdown_bundle.rglob("*.md")).read_text(encoding="utf-8")
            self.assertIn('usage:\n  source: "reported"\n  completeness: "partial"', markdown)
            self.assertIn("    input_tokens: 17", markdown)
            self.assertIn("    output_tokens: 7", markdown)
            document = json.loads(
                next(
                    item
                    for item in markdown_bundle.rglob("*.json")
                    if item.name != "metadata.json"
                ).read_text(encoding="utf-8")
            )
            exported_session = document["sessions"][0]
            self.assertEqual(len(exported_session["usage"]), 2)
            self.assertEqual(
                exported_session["usage_summary"]["turns_with_partial_usage"], 1
            )
            self.assertEqual(
                exported_session["usage_summary"]["coverage"]["fields"]["input_tokens"],
                {"reported_turns": 2, "selected_turns": 2},
            )
            metadata = json.loads(
                (markdown_bundle / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["usage"]["totals"]["input_tokens"], 17)
            self.assertEqual(metadata["usage"]["completeness"], "partial")

            jsonl_output = Path(tmp) / "jsonl-output"
            self.assertEqual(
                main(
                    [
                        "--source",
                        str(path),
                        "--output",
                        str(jsonl_output),
                        "--format",
                        "jsonl",
                        "--turns",
                        "1",
                        "--quiet",
                    ]
                ),
                EXIT_OK,
            )
            records = [
                json.loads(line)
                for line in (first_bundle(jsonl_output) / "history.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            usage_records = [
                record for record in records if record.get("record_type") == "usage"
            ]
            self.assertEqual(len(usage_records), 1)
            self.assertEqual(usage_records[0]["turn"], 1)
            self.assertEqual(usage_records[0]["usage"]["fields"]["total_tokens"]["value"], 14)
            self.assertEqual(
                sum(record.get("record_type") == "usage" for record in records), 1
            )

    def test_tool_event_parts_are_classified_and_kept_in_order(self) -> None:
        parts = extract_response_parts(
            [
                {"value": "before"},
                {
                    "kind": "toolInvocationSerialized",
                    "invocationMessage": {"value": "call input"},
                    "result": {"value": "call result"},
                    "toolSpecificData": {
                        "kind": "terminal",
                        "command": "echo hello",
                        "output": "hello",
                    },
                    "customMetadata": {"preserved": True},
                },
                {"kind": "textEditGroup", "edits": [{"text": "diff"}]},
                {
                    "kind": "inlineReference",
                    "inlineReference": {
                        "name": "example()",
                        "location": {
                            "uri": {"fsPath": "/project/example.py"},
                            "range": {"startLineNumber": 12, "endLineNumber": 12},
                        },
                    },
                },
                {"kind": "warning", "message": "warning"},
                {"kind": "futureEvent", "payload": {"retained": True}},
                {"value": "after"},
            ]
        )
        self.assertEqual(
            [part.kind for part in parts],
            [
                "answer",
                "tool_call",
                "tool_result",
                "tool_event",
                "edit",
                "inline_reference",
                "tool_event",
                "tool_event",
                "answer",
            ],
        )
        self.assertIn("customMetadata", parts[3].content)
        self.assertIn("echo hello", parts[1].content)
        self.assertIn('"output": "hello"', parts[2].content)
        self.assertIn("futureEvent", parts[7].content)
        id_only_call = ContentPart(
            "tool_call",
            json.dumps({"toolCallId": "opaque-call-id"}),
            label="Copilot tool call",
        )
        self.assertIn(
            "Call details were not stored",
            render_message([id_only_call], "assistant"),
        )

        session = Session(
            "sid",
            "ws",
            "Tool order",
            "agent",
            0.0,
            1.0,
            [
                Message(
                    "assistant",
                    "",
                    "2023-11-14T00:00:00Z",
                    1.0,
                    parts=parts,
                )
            ],
        )
        filtered, _ = filter_sessions(
            [session],
            FilterOptions(content=frozenset({"answers", "tool-results"})),
        )
        rendered = filtered[0].messages[0].content
        self.assertLess(rendered.index("before"), rendered.index("tool-calls omitted: call input"))
        self.assertLess(rendered.index("tool-calls omitted"), rendered.index("call result"))
        grouped_omission = rendered.index("tool-events, edits omitted")
        self.assertLess(rendered.index("call result"), grouped_omission)
        self.assertLess(
            grouped_omission, rendered.index("after")
        )
        self.assertNotIn("```json\n\n```", rendered)

    def test_conversation_excludes_tool_output_but_keeps_inline_references(self) -> None:
        parts = extract_response_parts(
            [
                {"value": "- "},
                {
                    "kind": "inlineReference",
                    "inlineReference": {"name": "first_feature()"},
                },
                {"value": " and "},
                {
                    "kind": "inlineReference",
                    "inlineReference": {"name": "second_feature()"},
                },
                {"value": " are implemented.\n"},
                {
                    "kind": "toolInvocationSerialized",
                    "invocationMessage": {"value": "Search README and source files"},
                    "pastTenseMessage": {
                        "value": "Searched for features, 31 results"
                    },
                    "resultDetails": [
                        {
                            "uri": {
                                "fsPath": "/workspace/src/cli.py",
                                "external": "file:///workspace/src/cli.py",
                            },
                            "range": {
                                "startLineNumber": 18,
                                "endLineNumber": 18,
                            },
                        },
                        {
                            "uri": {"fsPath": "/workspace/src/products.py"},
                            "range": {
                                "startLineNumber": 39,
                                "endLineNumber": 42,
                            },
                        },
                    ],
                    "isComplete": True,
                    "source": {"label": "Built-In"},
                },
            ]
        )
        self.assertEqual(
            [part.kind for part in parts],
            [
                "answer",
                "inline_reference",
                "answer",
                "inline_reference",
                "answer",
                "tool_call",
                "tool_result",
                "tool_event",
            ],
        )
        session = Session(
            "sid",
            "ws",
            "Tool summary",
            "agent",
            0.0,
            1.0,
            [
                Message(
                    "assistant",
                    "",
                    "2026-11-05T00:00:00Z",
                    1.0,
                    parts=parts,
                )
            ],
        )
        filtered, _ = filter_sessions(
            [session],
            FilterOptions(content=frozenset({"answers"})),
        )
        message = filtered[0].messages[0]
        self.assertIn("- `first_feature()` and `second_feature()` are implemented.", message.content)
        self.assertNotIn("### Tool result", message.content)
        self.assertNotIn("Searched for features, 31 results", message.content)
        self.assertNotIn("/workspace/src/cli.py:L18", message.content)
        self.assertNotIn("references omitted", message.content)
        self.assertFalse(any(part.kind in {"tool_call", "tool_result", "tool_event"} for part in message.parts))

    def test_inline_references_with_direct_windows_uri_render_in_list(self) -> None:
        parts = extract_response_parts(
            [
                {"value": "- "},
                {
                    "kind": "inlineReference",
                    "inlineReference": {
                        "$mid": 1,
                        "fsPath": r"C:\Users\Example\Projects\sample-project\src\alpha.sh",
                        "_sep": 1,
                        "external": "file:///c%3A/Users/Example/Projects/sample-project/src/alpha.sh",
                        "path": "/c:/Users/Example/Projects/sample-project/src/alpha.sh",
                        "scheme": "file",
                    },
                },
                {"value": "\n- "},
                {
                    "kind": "inlineReference",
                    "inlineReference": {
                        "fsPath": r"C:\Users\Example\Projects\sample-project\src\beta.sh",
                        "path": "/c:/Users/Example/Projects/sample-project/src/beta.sh",
                        "scheme": "file",
                    },
                },
            ]
        )

        self.assertEqual(
            [part.label for part in parts if part.kind == "inline_reference"],
            ["alpha.sh", "beta.sh"],
        )
        self.assertEqual(
            render_message(parts, "assistant"),
            "- `alpha.sh`\n- `beta.sh`",
        )

    def test_inline_references_with_trailing_directory_separator_render(self) -> None:
        parts = extract_response_parts(
            [
                {"value": "Sobre "},
                {
                    "kind": "inlineReference",
                    "inlineReference": {
                        "fsPath": r"C:\Users\Example\Projects\sample-project\build-output" + "\\",
                    },
                },
                {"value": " y "},
                {
                    "kind": "inlineReference",
                    "inlineReference": {
                        "fsPath": r"C:\Users\Example\Projects\sample-project\tools" + "\\",
                    },
                },
                {"value": ":"},
            ]
        )

        self.assertEqual(
            [part.label for part in parts if part.kind == "inline_reference"],
            ["build-output", "tools"],
        )
        self.assertEqual(
            render_message(parts, "assistant"),
            "Sobre `build-output` y `tools`:",
        )

    def test_conversation_inlines_serialized_inline_reference_parts(self) -> None:
        def inline_reference(name: str) -> ContentPart:
            return ContentPart(
                "reference",
                json.dumps(
                    {
                        "kind": "inlineReference",
                        "inlineReference": {"name": name, "kind": 12},
                    }
                ),
                label="inlineReference",
            )

        parts = [
            ContentPart("answer", "- Sobre "),
            inline_reference("prompts"),
            ContentPart("answer", " vs "),
            inline_reference("conversation"),
            ContentPart("answer", " = diálogo limpio, sin tool activity.\n"),
            ContentPart("answer", "- `prompts` = lo mismo pero con "),
            inline_reference('role="user"'),
            ContentPart("answer", " adicional.\n"),
            ContentPart("answer", "- Sobre "),
            inline_reference("memory"),
            ContentPart("answer", " / "),
            inline_reference("rag"),
            ContentPart("answer", ": se conserva el contexto generado.\n"),
        ]
        session = Session(
            "sid",
            "ws",
            "Serialized inline references",
            "agent",
            0.0,
            1.0,
            [
                Message(
                    "assistant",
                    "",
                    "2026-11-05T00:00:00Z",
                    1.0,
                    parts=parts,
                )
            ],
        )

        filtered, _ = filter_sessions(
            [session],
            FilterOptions(content=frozenset({"prompts", "answers"})),
        )
        content = filtered[0].messages[0].content

        self.assertIn(
            "- Sobre `prompts` vs `conversation` = diálogo limpio, sin tool activity.",
            content,
        )
        self.assertIn('- `prompts` = lo mismo pero con `role="user"` adicional.', content)
        self.assertIn("- Sobre `memory` / `rag`: se conserva el contexto generado.", content)
        self.assertNotIn("### Reference", content)

    def test_nested_tool_specific_result_is_readable_in_markdown(self) -> None:
        parts = extract_response_parts(
            [
                {
                    "kind": "toolInvocationSerialized",
                    "toolSpecificData": {
                        "pastTenseMessage": {"value": "Read the project config"},
                        "resultDetails": [
                            {
                                "uri": {"fsPath": "/workspace/config.toml"},
                                "range": {"startLineNumber": 7, "endLineNumber": 9},
                            }
                        ],
                    },
                }
            ]
        )
        session = Session(
            "sid",
            "ws",
            "Nested tool result",
            "agent",
            0.0,
            1.0,
            [
                Message(
                    "assistant",
                    "",
                    "2026-11-05T00:00:00Z",
                    1.0,
                    parts=parts,
                )
            ],
        )
        filtered, _ = filter_sessions(
            [session],
            FilterOptions(content=frozenset({"tool-results"})),
        )
        content = filtered[0].messages[0].content
        self.assertIn("Read the project config", content)
        self.assertIn("/workspace/config.toml:L7-L9", content)
        self.assertNotIn('"toolSpecificData"', content)

    def test_no_omission_markers_remove_empty_fence_left_by_filtered_tool(self) -> None:
        session = Session(
            "sid",
            "ws",
            "Empty fence",
            "agent",
            0.0,
            1.0,
            [
                Message(
                    "assistant",
                    "",
                    "2026-11-05T00:00:00Z",
                    1.0,
                    parts=[
                        ContentPart("answer", "Before\n````python\n"),
                        ContentPart("tool_call", '{"invocationMessage": "run"}'),
                        ContentPart(
                            "answer",
                            "\n````\nAfter\n\n````python\nkept = True\n````",
                        ),
                    ],
                )
            ],
        )
        filtered, _ = filter_sessions(
            [session],
            FilterOptions(
                content=frozenset({"answers"}),
                omission_markers=False,
            ),
        )
        content = filtered[0].messages[0].content
        self.assertIn("Before", content)
        self.assertIn("After", content)
        self.assertIn("````python\nkept = True\n````", content)
        self.assertNotIn("````python\n\n````", content)
        self.assertNotIn("omitted", content)

    def test_conversation_preset_and_compact_omission_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp) / "fixture")
            output = Path(tmp) / "output"
            self.assertEqual(
                main(
                    [
                        "--source",
                        str(fixture["root"]),
                        "--output",
                        str(output),
                        "--format",
                        "markdown",
                        "--conversation",
                        "--quiet",
                    ]
                ),
                EXIT_OK,
            )
            bundle = first_bundle(output)
            metadata = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(
                metadata["filters"]["content"],
                ["answers", "prompts", "reasoning"],
            )
            self.assertGreater(
                metadata["filters"]["statistics"]["parts"]["emitted"]["reasoning"],
                0,
            )
            omissions = metadata["filters"]["statistics"]["omissions"]
            self.assertTrue(omissions)
            self.assertTrue(
                all(
                    {"session_id", "turn", "message_index", "part_indices", "categories"}
                    <= omission.keys()
                    for omission in omissions
                )
            )
            markdown = "\n".join(
                path.read_text(encoding="utf-8") for path in bundle.rglob("*.md")
            )
            self.assertIn("## Reasoning", markdown)
            self.assertIn("private reasoning", markdown)
            self.assertIn("tool-calls, tool-results omitted", markdown)
            self.assertNotIn("### Tool result", markdown)
            self.assertNotIn("Edit/diff", markdown)

    def test_no_omission_markers_keeps_turn_notices_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp) / "fixture")
            output = Path(tmp) / "output"
            self.assertEqual(
                main(
                    [
                        "--source",
                        str(fixture["root"]),
                        "--output",
                        str(output),
                        "--format",
                        "markdown",
                        "--format",
                        "jsonl",
                        "--conversation",
                        "--no-omission-markers",
                        "--turns",
                        "2",
                        "--quiet",
                    ]
                ),
                EXIT_OK,
            )
            bundle = first_bundle(output)
            markdown = "\n".join(
                path.read_text(encoding="utf-8") for path in bundle.rglob("*.md")
            )
            self.assertIn("turn 1 omitted", markdown)
            self.assertNotIn("tool-calls omitted", markdown)

            records = [
                json.loads(line)
                for line in (bundle / "history.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(record["role"] == "omission" for record in records))
            self.assertFalse(
                any(
                    record["content_type"] == "omission"
                    and record["role"] != "omission"
                    for record in records
                )
            )

            metadata = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
            self.assertFalse(metadata["filters"]["omission_markers"])
            self.assertEqual(
                metadata["filters"]["content"],
                ["answers", "prompts", "reasoning"],
            )
            omissions = metadata["filters"]["statistics"]["omissions"]
            self.assertTrue(omissions)
            self.assertTrue(any(omission.get("reason") == "turn-filter" for omission in omissions))
            self.assertTrue(any("reason" not in omission for omission in omissions))

    def test_explicit_content_selection_survives_no_omission_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp) / "fixture")
            output = Path(tmp) / "output"
            self.assertEqual(
                main(
                    [
                        "--source",
                        str(fixture["root"]),
                        "--output",
                        str(output),
                        "--format",
                        "markdown",
                        "--format",
                        "json",
                        "--content",
                        "prompts",
                        "--content",
                        "answers",
                        "--content",
                        "reasoning",
                        "--no-omission-markers",
                        "--quiet",
                    ]
                ),
                EXIT_OK,
            )
            bundle = first_bundle(output)
            metadata = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(
                metadata["filters"]["content"],
                ["answers", "prompts", "reasoning"],
            )
            emitted = metadata["filters"]["statistics"]["parts"]["emitted"]
            for category in ("answers", "prompts", "reasoning"):
                self.assertGreater(emitted.get(category, 0), 0)
            markdown = "\n".join(
                path.read_text(encoding="utf-8") for path in bundle.rglob("*.md")
            )
            self.assertIn("First question", markdown)
            self.assertIn("First answer", markdown)
            self.assertIn("private reasoning", markdown)

    def test_response_append_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            first = {
                "kind": 0,
                "v": {
                    "creationDate": 1700000000000,
                    "sessionId": "sid",
                    "requests": [{"timestamp": 1700000001000, "message": {"text": "Q"}, "response": []}],
                },
            }
            patch = {"kind": 2, "k": ["requests", 0, "response"], "v": [{"value": "A"}]}
            path.write_text(
                json.dumps(first) + "\n" + json.dumps(patch) + "\n",
                encoding="utf-8",
            )
            session = read_jsonl_sessions(path, workspace_for_path(path))[0]
            self.assertEqual([m.content for m in session.messages], ["Q", "A"])

    def test_response_list_of_strings(self) -> None:
        parts = extract_response_parts(["hello", {"value": "world"}])
        self.assertEqual(
            [(part.kind, part.content) for part in parts],
            [("answer", "hello"), ("answer", "world")],
        )

    def test_session_without_messages_is_skipped(self) -> None:
        state = {"creationDate": 1700000000000, "sessionId": "empty", "requests": []}
        workspace = workspace_for_path(Path("/tmp/example/chatSessions"))
        self.assertIsNone(session_from_state(state, workspace))


class AssemblyTests(unittest.TestCase):
    def test_concurrent_runs_reserve_distinct_output_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "exports"
            with ThreadPoolExecutor(max_workers=12) as executor:
                paths = list(
                    executor.map(
                        lambda _index: create_run_directory(
                            root, "20261108T120000Z", "reasoning"
                        ),
                        range(12),
                    )
                )
            self.assertEqual(len(set(paths)), 12)
            self.assertTrue(all(path.is_dir() for path in paths))

    def test_default_discovery_uses_known_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp) / "fixture")
            with patch(
                "copilot_exporter.discovery.default_user_dirs",
                return_value=[fixture["root"]],
            ):
                sources = discover()
            self.assertEqual(len(sources.jsonls), 2)
            self.assertEqual(len(sources.dbs), 2)
            self.assertEqual(len(sources.agent_sessions), 1)

    def test_windows_default_discovery_includes_windsurf_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            appdata = Path(tmp) / "Roaming"
            windsurf_user = appdata / "Windsurf" / "User"
            windsurf_user.mkdir(parents=True)
            fake_os = SimpleNamespace(
                name="nt",
                environ={"APPDATA": str(appdata), "LOCALAPPDATA": ""},
            )
            with (
                patch("copilot_exporter.discovery.os", fake_os),
                patch("copilot_exporter.discovery.Path", PosixPath),
            ):
                self.assertEqual(default_user_dirs(), [windsurf_user])

    def test_hybrid_sources_and_enrichment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp))
            sources = discover(str(fixture["root"]))
            assembled = assemble(sources)

            self.assertEqual(len(assembled.sessions), 3)
            by_id = {session.session_id: session for session in assembled.sessions}
            project = by_id[fixture["session_id"]]
            self.assertEqual(project.title, "Indexed project chat")
            self.assertEqual(project.created_at, "2023-11-14T22:15:00Z")
            self.assertEqual(len(project.messages), 4)

            db_only = [
                session
                for session in assembled.sessions
                if session.session_id == fixture["agent_session_id"]
            ]
            self.assertEqual(len(db_only), 1)
            self.assertEqual(
                [message.role for message in db_only[0].messages],
                ["user", "assistant", "user", "assistant"],
            )
            self.assertEqual(
                [message.content for message in db_only[0].messages if message.role == "user"],
                ["DB prompt one", "DB prompt two"],
            )
            self.assertIn("Agent answer one.", db_only[0].messages[1].content)
            self.assertIn("echo hello", db_only[0].messages[1].content)
            self.assertIn("terminal", db_only[0].messages[1].content)
            self.assertIn("terminal output one", db_only[0].messages[1].content)
            self.assertIn("Agent answer two.", db_only[0].messages[3].content)
            self.assertIn(
                "unreferenced terminal output", db_only[0].messages[3].content
            )
            self.assertIn("resource result", db_only[0].messages[3].content)
            self.assertEqual(db_only[0].title, "Agent-backed chat")

            created = [session.created_ts for session in assembled.sessions]
            self.assertEqual(created, sorted(created))


class CliTests(unittest.TestCase):
    def test_raw_reasoning_flag_controls_serialized_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "session.jsonl"
            request = _request(1, "Question", "Answer", 1700000001000)
            raw_reasoning = {
                "kind": "thinking",
                "value": "",
                "id": "opaque-id",
                "metadata": {"vscodeReasoningDone": True},
            }
            request["response"].append(raw_reasoning)
            write_jsonl(source, "sid", [request])

            default_output = root / "default"
            self.assertEqual(
                main(
                    [
                        "--source",
                        str(source),
                        "--output",
                        str(default_output),
                        "--format",
                        "markdown",
                        "--content",
                        "reasoning",
                        "--quiet",
                    ]
                ),
                EXIT_OK,
            )
            default_bundle = first_bundle(default_output)
            default_markdown = next(default_bundle.rglob("*.md")).read_text(
                encoding="utf-8"
            )
            default_metadata = json.loads(
                (default_bundle / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertIn("private reasoning", default_markdown)
            self.assertNotIn("opaque-id", default_markdown)
            self.assertIn("serialized_reasoning omitted", default_markdown)
            self.assertFalse(default_metadata["filters"]["include_raw_reasoning"])

            raw_output = root / "raw"
            self.assertEqual(
                main(
                    [
                        "--source",
                        str(source),
                        "--output",
                        str(raw_output),
                        "--format",
                        "markdown",
                        "--content",
                        "reasoning",
                        "--include-raw-reasoning",
                        "--quiet",
                    ]
                ),
                EXIT_OK,
            )
            raw_bundle = first_bundle(raw_output)
            raw_markdown = next(raw_bundle.rglob("*.md")).read_text(encoding="utf-8")
            raw_metadata = json.loads(
                (raw_bundle / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertIn("opaque-id", raw_markdown)
            self.assertTrue(raw_metadata["filters"]["include_raw_reasoning"])

    def test_default_and_markdown_exports_keep_readable_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "session.jsonl"
            request = _request(1, "Question", "Answer", 1700000001000)
            request["response"].append(
                {
                    "kind": "thinking",
                    "value": "",
                    "id": "opaque-id",
                    "metadata": {"vscodeReasoningDone": True},
                }
            )
            write_jsonl(source, "sid", [request])

            for label, format_args in (
                ("default", []),
                ("markdown", ["--format", "markdown"]),
            ):
                output = root / label
                self.assertEqual(
                    main(
                        [
                            "--source",
                            str(source),
                            "--output",
                            str(output),
                            *format_args,
                            "--quiet",
                        ]
                    ),
                    EXIT_OK,
                )
                bundle = first_bundle(output)
                markdown = next(bundle.rglob("*.md")).read_text(encoding="utf-8")
                metadata = json.loads(
                    (bundle / "metadata.json").read_text(encoding="utf-8")
                )
                self.assertIn("private reasoning", markdown)
                self.assertIn("serialized_reasoning omitted", markdown)
                self.assertNotIn("opaque-id", markdown)
                self.assertGreater(
                    metadata["filters"]["statistics"]["parts"]["emitted"][
                        "reasoning"
                    ],
                    0,
                )

                if label == "default":
                    document = json.loads(
                        next(
                            path
                            for path in bundle.rglob("*.json")
                            if path.name != "metadata.json"
                        ).read_text(encoding="utf-8")
                    )
                    reasoning = [
                        part
                        for message in document["sessions"][0]["messages"]
                        for part in message["parts"]
                        if part["type"] == "reasoning"
                    ]
                    self.assertTrue(
                        any(part["content"] == "private reasoning" for part in reasoning)
                    )
                    self.assertFalse(
                        any("opaque-id" in part["content"] for part in reasoning)
                    )

    def test_cli_exposes_one_content_filter_and_clear_progress_behavior(self) -> None:
        help_text = build_parser().format_help()
        for removed_option in (
            "--prompts-only",
            "--tools-only",
            "--without-reasoning",
            "--without-tools",
            "--append",
            "--overwrite",
            "--no-overwrite",
            "--date-suffix",
        ):
            self.assertNotIn(removed_option, help_text)
        self.assertIn("automatic in terminals", help_text)
        self.assertIn("--last-turns", help_text)

    def test_turn_selection_options_are_mutually_exclusive(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                parser.parse_args(["--turns", "1", "--last-turns", "1"])
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                parser.parse_args(["--content", "prompts", "--conversation"])

    def test_last_turns_keeps_original_turn_numbers_in_json(self) -> None:
        session = Session(
            "sid",
            "ws",
            "Three turns",
            "agent",
            0.0,
            3.0,
            [
                Message("user", f"prompt {turn}", sort_key=turn * 2 - 1, turn=turn)
                if role == "user"
                else Message(
                    "assistant",
                    f"answer {turn}",
                    sort_key=turn * 2,
                    parts=[ContentPart("answer", f"answer {turn}")],
                    turn=turn,
                )
                for turn in range(1, 4)
                for role in ("user", "assistant")
            ],
        )
        filtered, stats = filter_sessions(
            [session],
            FilterOptions(last_turns=1),
        )
        self.assertEqual([message.turn for message in filtered[0].messages], [3, 3, 3])
        self.assertEqual(
            [message["turn"] for message in filtered[0].as_dict("ws")["messages"]],
            [3, 3, 3],
        )
        self.assertEqual(filtered[0].messages[0].role, "omission")
        self.assertIn("turns 1-2 omitted", filtered[0].messages[0].content)
        self.assertEqual(stats.available_turns, 3)
        self.assertEqual(stats.selected_turns, 1)

        discontinuous, _ = filter_sessions(
            [session],
            FilterOptions(turns=frozenset({1, 3})),
        )
        roles = [message.role for message in discontinuous[0].messages]
        self.assertEqual(roles, ["user", "assistant", "omission", "user", "assistant"])
        self.assertIn("turn 2 omitted", discontinuous[0].messages[2].content)

    def test_last_turns_are_applied_to_markdown_and_session_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp) / "fixture")
            output = Path(tmp) / "output"
            self.assertEqual(
                main(
                    [
                        "--source",
                        str(fixture["root"]),
                        "--output",
                        str(output),
                        "--format",
                        "markdown",
                        "--format",
                        "json",
                        "--last-turns",
                        "1",
                        "--quiet",
                    ]
                ),
                EXIT_OK,
            )
            bundle = first_bundle(output)
            project = next(
                item
                for path in bundle.rglob("*.json")
                if path.name != "metadata.json"
                for item in json.loads(path.read_text(encoding="utf-8")).get(
                    "sessions", []
                )
                if item["session_id"] == fixture["session_id"]
            )
            self.assertEqual({message["turn"] for message in project["messages"]}, {2})
            self.assertTrue(
                any("turn 1 omitted" in message["content"] for message in project["messages"])
            )
            markdown = "\n".join(
                path.read_text(encoding="utf-8") for path in bundle.rglob("*.md")
            )
            self.assertIn("turn 1 omitted", markdown)
            metadata = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["filters"]["last_turns"], 1)

    def test_each_export_run_writes_an_isolated_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp) / "fixture")
            output = Path(tmp) / "output"
            args = [
                "--source",
                str(fixture["root"]),
                "--output",
                str(output),
                "--format",
                "markdown",
                "--format",
                "json",
            ]

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(args), EXIT_OK)
            self.assertIn("Sessions exported: 3", stdout.getvalue())

            bundle = first_bundle(output)
            self.assertRegex(bundle.name, r"^\d{8}T\d{6}Z_full$")
            json_files = [
                path
                for path in bundle.rglob("*.json")
                if not path.name.startswith(".") and path.name != "metadata.json"
            ]
            md_files = list(bundle.rglob("*.md"))
            self.assertGreaterEqual(len(json_files), 3)
            self.assertGreaterEqual(len(md_files), 3)
            self.assertTrue(
                any(path.name == "2023-11-14__project__abcdef01" for path in bundle.iterdir())
            )
            self.assertTrue(
                any(path.name == "2023-11-14__Indexed project chat.md" for path in md_files)
            )

            markdown = next(
                path for path in md_files if fixture["session_id"] in path.read_text(encoding="utf-8")
            ).read_text(encoding="utf-8")
            self.assertTrue(markdown.startswith("---\nsession_id:"))
            self.assertIn(
                "# 2023-11-14 — Indexed project chat",
                markdown,
            )
            self.assertNotIn("# Copilot chat history", markdown)
            self.assertIn("### User", markdown)
            self.assertIn("### Assistant", markdown)
            self.assertIn("## Reasoning", markdown)
            self.assertIn("### Tool call/input", markdown)
            self.assertIn("### Tool result", markdown)

            project_json = next(
                path
                for path in json_files
                if path.name != "metadata.json" and fixture["session_id"] in path.read_text(encoding="utf-8")
            )
            payload = json.loads(project_json.read_text(encoding="utf-8"))
            self.assertEqual(set(payload), {"export_date", "source_env", "total_sessions", "sessions"})
            self.assertEqual(payload["sessions"][0]["session_id"], fixture["session_id"])

            second_stdout = io.StringIO()
            with redirect_stdout(second_stdout):
                self.assertEqual(main(args), EXIT_OK)
            self.assertIn("Sessions exported: 3", second_stdout.getvalue())
            bundles = sorted(path for path in output.iterdir() if path.is_dir())
            self.assertEqual(len(bundles), 2)
            self.assertNotEqual(bundles[0], bundles[1])
            self.assertTrue(all((path / "metadata.json").is_file() for path in bundles))
            self.assertFalse((output / ".copilot_export_manifest.json").exists())

    def test_jsonl_is_one_stream_alongside_per_session_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp) / "fixture")
            output = Path(tmp) / "output"
            result = main(
                [
                    "--source",
                    str(fixture["root"]),
                    "--output",
                    str(output),
                    "--format",
                    "markdown",
                    "--format",
                    "jsonl",
                    "--quiet",
                ]
            )
            self.assertEqual(result, EXIT_OK)
            bundle = first_bundle(output)
            markdown_files = list(bundle.rglob("*.md"))
            self.assertEqual(len(markdown_files), 3)
            self.assertTrue((bundle / "history.jsonl").is_file())
            records = [
                json.loads(line)
                for line in (bundle / "history.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                len({record["session_id"] for record in records}),
                3,
            )
            metadata = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
            self.assertNotIn("merged", metadata)
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                build_parser().parse_args(["--merge"])
            self.assertEqual(error.exception.code, 2)

    def test_jsonl_content_turn_filter_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp) / "fixture")
            output = Path(tmp) / "output"
            result = main(
                [
                    "--source",
                    str(fixture["root"]),
                    "--output",
                    str(output),
                    "--format",
                    "jsonl",
                    "--turns",
                    "2",
                    "--content",
                    "prompts",
                    "--content",
                    "tool-results",
                    "--max-tool-output-chars",
                    "60",
                    "--quiet",
                ]
            )
            self.assertEqual(result, EXIT_OK)
            bundle = first_bundle(output)
            records = [
                json.loads(line)
                for line in (bundle / "history.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(records)
            self.assertEqual({record["turn"] for record in records}, {2})
            self.assertEqual(
                {record["content_type"] for record in records},
                {"prompt", "tool_result", "omission"},
            )
            self.assertTrue(any(record["truncated"] for record in records))
            metadata = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["filters"]["turns"], ["2"])
            self.assertEqual(metadata["filters"]["content"], ["prompts", "tool-results"])
            self.assertGreater(
                metadata["filters"]["statistics"]["parts"]["truncated"]["tool-results"], 0
            )

    def test_truncation_direction_preserves_requested_end_and_limit(self) -> None:
        original = "0123456789" * 10
        session = Session(
            "sid",
            "ws",
            "Truncation test",
            "agent",
            0.0,
            1.0,
            [
                Message(
                    "assistant",
                    original,
                    "2023-11-14T00:00:00Z",
                    1.0,
                    parts=[ContentPart("tool_output", original)],
                )
            ],
        )
        tail, _ = filter_sessions(
            [session],
            FilterOptions(
                content=frozenset({"tool-results"}),
                max_tool_output_chars=60,
                truncate_direction="beginning",
            ),
        )
        head, _ = filter_sessions(
            [session],
            FilterOptions(
                content=frozenset({"tool-results"}),
                max_tool_output_chars=60,
                truncate_direction="end",
            ),
        )
        tail_content = tail[0].messages[0].parts[0].content
        head_content = head[0].messages[0].parts[0].content
        self.assertTrue(tail_content.startswith(original[:10]))
        self.assertIn("[... truncated 40 characters; kept first 60]", tail_content)
        self.assertIn("[... truncated 40 characters; kept last 60]", head_content)
        self.assertTrue(head_content.endswith(original[-60:]))

    def test_tool_output_truncation_is_opt_in_and_defaults_to_end(self) -> None:
        args = build_parser().parse_args(["--source", "unused"])
        self.assertIsNone(args.max_tool_output_chars)
        self.assertEqual(args.truncate_direction, "end")

        original = "0123456789" * 2500
        session = Session(
            "sid",
            "ws",
            "Untruncated tool output",
            "agent",
            0.0,
            1.0,
            [
                Message(
                    "assistant",
                    original,
                    "2023-11-14T00:00:00Z",
                    1.0,
                    parts=[ContentPart("tool_output", original)],
                )
            ],
        )
        unchanged, _ = filter_sessions(
            [session], FilterOptions(content=frozenset({"tool-results"}))
        )
        unchanged_part = unchanged[0].messages[0].parts[0]
        self.assertEqual(unchanged_part.content, original)
        self.assertFalse(unchanged_part.truncated)

        truncated, _ = filter_sessions(
            [session],
            FilterOptions(
                content=frozenset({"tool-results"}),
                max_tool_output_chars=20_000,
            ),
        )
        truncated_content = truncated[0].messages[0].parts[0].content
        self.assertTrue(truncated_content.startswith("[... truncated 5000 characters; kept last 20000]"))
        self.assertTrue(truncated_content.endswith(original[-20_000:]))

    def test_multiple_formats_write_session_json_and_history_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp) / "fixture")
            output = Path(tmp) / "output"
            result = main(
                [
                    "--source",
                    str(fixture["root"]),
                    "--output",
                    str(output),
                    "--format",
                    "json",
                    "--format",
                    "jsonl",
                    "--jsonl-compact",
                    "--quiet",
                ]
            )
            self.assertEqual(result, EXIT_OK)
            bundle = first_bundle(output)
            session_json_files = [
                path
                for path in bundle.rglob("*.json")
                if path.name != "metadata.json" and not path.name.startswith(".")
            ]
            self.assertEqual(len(session_json_files), 3)
            self.assertTrue((bundle / "history.jsonl").is_file())
            records = [
                json.loads(line)
                for line in (bundle / "history.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(record["type"] == "session" for record in records))
            metadata = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["formats"], ["json", "jsonl"])
            self.assertTrue(
                all(
                    any(path.endswith(".json") for path in files)
                    and "history.jsonl" in files
                    for files in metadata["session_files"].values()
                )
            )

    def test_json_layout_option_is_removed(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            build_parser().parse_args(["--json-layout", "both"])
        self.assertEqual(error.exception.code, 2)

    def test_jsonl_is_a_direct_output_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp) / "fixture")
            output = Path(tmp) / "output"
            self.assertEqual(
                main(
                    [
                        "--source",
                        str(fixture["root"]),
                        "--output",
                        str(output),
                        "--format",
                        "jsonl",
                        "--quiet",
                    ]
                ),
                EXIT_OK,
            )
            bundle = first_bundle(output)
            self.assertTrue((bundle / "history.jsonl").is_file())
            self.assertFalse(
                [
                    path
                    for path in bundle.rglob("*.json")
                    if path.name != "metadata.json"
                ]
            )
            metadata = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["formats"], ["jsonl"])

    def test_usage_summary_coverage_uses_selected_turns(self) -> None:
        session = Session(
            "usage-session",
            "workspace",
            "Usage coverage",
            "agent",
            1.0,
            3.0,
            [
                Message("user", f"prompt {turn}", None, turn, turn=turn)
                for turn in (1, 2, 3)
            ],
            usage=[
                UsageRecord(
                    3,
                    fields={
                        "input_tokens": {"value": 20},
                        "output_tokens": {"value": 5},
                        "total_tokens": {"value": 25},
                    },
                )
            ],
        )
        filtered, _ = filter_sessions(
            [session],
            FilterOptions(content=frozenset({"prompts"}), turns=frozenset({3})),
        )
        summary = _usage_summary(filtered)
        self.assertEqual(summary["completeness"], "complete")
        self.assertEqual(summary["turns_with_usage"], 1)
        self.assertEqual(summary["turns_without_usage"], 0)
        self.assertEqual(summary["coverage"]["total_turns"], 1)
        self.assertEqual(
            summary["coverage"]["fields"]["input_tokens"],
            {"reported_turns": 1, "selected_turns": 1},
        )

    def test_megabyte_split_limit_and_content_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp) / "fixture")
            output = Path(tmp) / "output"
            self.assertEqual(
                main(
                    [
                        "--source",
                        str(fixture["root"]),
                        "--output",
                        str(output),
                        "--format",
                        "markdown",
                        "--content",
                        "prompts",
                        "--split-long-sessions",
                        "--max-session-md-mb",
                        "2",
                        "--quiet",
                    ]
                ),
                EXIT_OK,
            )
            bundle = first_bundle(output)
            metadata = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["layout"]["max_session_md_bytes"], 2_000_000)
            markdown = "\n".join(
                path.read_text(encoding="utf-8") for path in bundle.rglob("*.md")
            )
            self.assertIn("First question", markdown)
            self.assertNotIn("First answer", markdown)

    def test_default_writes_markdown_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp) / "fixture")
            output = Path(tmp) / "output"
            self.assertEqual(
                main(["--source", str(fixture["root"]), "--output", str(output), "--quiet"]),
                EXIT_OK,
            )
            bundle = first_bundle(output)
            self.assertTrue(list(bundle.rglob("*.md")))
            self.assertTrue(
                [
                    path
                    for path in bundle.rglob("*.json")
                    if path.name != "metadata.json" and not path.name.startswith(".")
                ]
            )

    def test_filter_variants_create_independent_labeled_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp) / "fixture")
            output = Path(tmp) / "output"
            base_args = [
                "--source",
                str(fixture["root"]),
                "--output",
                str(output),
                "--format",
                "markdown",
                "--quiet",
            ]
            self.assertEqual(main(base_args + ["--content", "prompts"]), EXIT_OK)
            self.assertEqual(main(base_args + ["--content", "answers"]), EXIT_OK)
            self.assertEqual(main(base_args + ["--content", "prompts"]), EXIT_OK)

            bundles = sorted(path for path in output.iterdir() if path.is_dir())
            labels_by_bundle = {
                path: json.loads((path / "metadata.json").read_text(encoding="utf-8"))[
                    "run_label"
                ]
                for path in bundles
            }
            labels = sorted(labels_by_bundle.values())
            self.assertEqual(labels, ["answers", "prompts", "prompts"])
            self.assertEqual(len({path.name for path in bundles}), 3)
            answers_bundle = next(
                path for path, label in labels_by_bundle.items() if label == "answers"
            )
            prompts_bundle = next(
                path for path, label in labels_by_bundle.items() if label == "prompts"
            )
            answers = "\n".join(
                path.read_text(encoding="utf-8")
                for path in answers_bundle.rglob("*.md")
            )
            prompts = "\n".join(
                path.read_text(encoding="utf-8")
                for path in prompts_bundle.rglob("*.md")
            )
            self.assertIn("First answer", answers)
            self.assertNotIn("First question", answers)
            self.assertIn("First question", prompts)
            self.assertNotIn("First answer", prompts)
            self.assertTrue(all((bundle / "metadata.json").is_file() for bundle in bundles))
            self.assertTrue(
                all(
                    (bundle / relative).is_file()
                    for bundle in bundles
                    for relative in json.loads(
                        (bundle / "metadata.json").read_text(encoding="utf-8")
                    )["written_files"]
                )
            )

    def test_jsonl_compact_uses_session_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp) / "fixture")
            output = Path(tmp) / "output"
            self.assertEqual(
                main(
                    [
                        "--source",
                        str(fixture["root"]),
                        "--output",
                        str(output),
                        "--format",
                        "jsonl",
                        "--jsonl-compact",
                        "--quiet",
                    ]
                ),
                EXIT_OK,
            )
            records = [
                json.loads(line)
                for line in (first_bundle(output) / "history.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            headers = [record for record in records if record["type"] == "session"]
            parts = [record for record in records if record["type"] == "part"]
            self.assertTrue(headers)
            self.assertTrue(parts)
            self.assertTrue(all("title" in record for record in headers))
            self.assertTrue(all("title" not in record for record in parts))
            self.assertTrue(all("workspace" not in record for record in parts))

    def test_split_long_markdown_preserves_messages(self) -> None:
        messages = [
            Message("user", "A" * 120, "2023-11-14T00:00:00Z", 1.0),
            Message("assistant", "B" * 120, "2023-11-14T00:00:01Z", 2.0),
            Message("user", "C" * 120, "2023-11-14T00:00:02Z", 3.0),
        ]
        session = Session("sid", "ws", "Long chat", "agent", 0.0, 3.0, messages)
        parts = split_markdown_session(session, "workspace", 500)
        self.assertGreater(len(parts), 1)
        combined = "\n".join(parts)
        self.assertEqual(combined.count("A" * 120), 1)
        self.assertEqual(combined.count("B" * 120), 1)
        self.assertEqual(combined.count("C" * 120), 1)
        self.assertTrue(all(len(part.encode("utf-8")) <= 500 for part in parts))

    def test_split_long_markdown_is_indexed_in_run_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp) / "fixture")
            output = Path(tmp) / "output"
            args = [
                "--source",
                str(fixture["root"]),
                "--output",
                str(output),
                "--format",
                "markdown",
                "--split-long-sessions",
                "--max-session-md-bytes",
                "300",
                "--quiet",
            ]
            self.assertEqual(main(args), EXIT_OK)
            bundle = first_bundle(output)
            metadata = json.loads(
                (bundle / "metadata.json").read_text(encoding="utf-8")
            )
            parts = metadata["session_files"][fixture["session_id"]]
            self.assertGreater(len(parts), 1)
            self.assertTrue(all((bundle / part).is_file() for part in parts))
            text = "\n".join((bundle / part).read_text(encoding="utf-8") for part in parts)
            self.assertEqual(text.count("First question"), 1)
            self.assertEqual(text.count("Second question"), 1)

            self.assertEqual(main(args), EXIT_OK)
            self.assertEqual(len([path for path in output.iterdir() if path.is_dir()]), 2)

    def test_dry_run_plans_bundle_without_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp) / "fixture")
            output = Path(tmp) / "new-output"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    main(
                        [
                            "--source",
                            str(fixture["root"]),
                            "--output",
                            str(output),
                            "--content",
                            "prompts",
                            "--label",
                            "before-review",
                            "--dry-run",
                        ]
                    ),
                    EXIT_OK,
                )
            self.assertFalse(output.exists())
            self.assertIn("_before-review", stdout.getvalue())
            self.assertIn("metadata.json", stdout.getvalue())
            self.assertIn("Planned files:", stdout.getvalue())

    def test_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--source", str(missing)]), EXIT_SOURCE_ERROR)

            empty = Path(tmp) / "empty" / "workspaceStorage" / "hash"
            connection = create_item_table(empty / "state.vscdb")
            connection.commit()
            connection.close()
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--source", str(empty)]), EXIT_NO_DATA)

    def test_quiet_suppresses_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp) / "fixture")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    main(
                        [
                            "--source",
                            str(fixture["root"]),
                            "--output",
                            str(Path(tmp) / "out"),
                            "--quiet",
                        ]
                    ),
                    EXIT_OK,
                )
            self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
