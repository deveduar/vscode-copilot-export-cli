# VS Code Copilot Export CLI

A cross-platform CLI for exporting locally stored GitHub Copilot chats from VS Code and compatible editor profiles. It uses Python's standard library, reads chat databases in read-only mode, and never writes to the editor's storage.

## Features

- Reads both Copilot's modern `agentSessionData` SQLite sessions and VS Code chat-session JSONL files.
- Combines matching records from available local stores and best-effort prompt history.
- Preserves inline references in assistant text and exports prompts, answers, reasoning, tool calls/results, edits, and other stored parts as selectable categories.
- Preserves token-usage data reported by the source.
- Writes Markdown and JSON per session, or a run-wide JSONL stream of typed parts and per-turn usage records, with a metadata index for each run.

An export represents the data the editor persisted and this tool can recognize. Separate prompt history may not be associable with a particular assistant session. Inline references in assistant text are rendered as readable labels to keep the prose flowing; Markdown does not promise to reproduce their full URI or path.

This is an independent community project and is not affiliated with, endorsed by, or sponsored by Microsoft, GitHub, Visual Studio Code, or GitHub Copilot. All product names and trademarks belong to their respective owners.

## Quick start

Clone the repository, then run from its root. Python 3.10 or later is required; no third-party packages need to be installed.

```bash
git clone https://github.com/deveduar/vscode-copilot-export-cli.git
cd vscode-copilot-exporter
python3 copilot_export.py
```

With no arguments, the exporter checks known VS Code, VS Code Server, code-server, VSCodium, Cursor, and Windsurf profile locations. Each run creates a new, timestamped bundle under `./copilot_export`; existing bundles are never overwritten.

For a concise Markdown conversation that keeps prompts, assistant answers, stored reasoning, and inline references while omitting standalone tool output:

```bash
python3 copilot_export.py --format markdown --conversation
```

Use `--source` for a file or directory, or `--db` for a `state.vscdb`. See [Input discovery and platform scope](#input-discovery-and-platform-scope).

## Command reference

```bash
# Default: export all selected content as Markdown and per-session JSON.
python3 copilot_export.py

# Show options.
python3 copilot_export.py --help

# Choose output formats; repeat --format to combine them.
python3 copilot_export.py --format markdown
python3 copilot_export.py --format markdown --format jsonl
python3 copilot_export.py --format markdown --format json --format jsonl

# Write compact JSONL records with shared session metadata in one header.
python3 copilot_export.py --format jsonl --jsonl-compact

# Export a conversation (prompts, answers, and readable reasoning).
python3 copilot_export.py --conversation

# Select content explicitly; repeat --content to combine categories.
python3 copilot_export.py --content prompts --content answers --content reasoning
python3 copilot_export.py --content tool-results --format jsonl
python3 copilot_export.py --content tool-calls --content edits --content references
python3 copilot_export.py --format markdown --conversation --no-omission-markers

# Include serialized reasoning blocks that have no readable text.
python3 copilot_export.py --include-raw-reasoning

# Give the bundle a descriptive label.
python3 copilot_export.py --content prompts --label prompts-review

# Limit turns.
python3 copilot_export.py --turns 1-3,7,10-
python3 copilot_export.py --last-turns 3

# Truncate reasoning or tool output; direction defaults to end.
python3 copilot_export.py --max-reasoning-chars 12000
python3 copilot_export.py --max-tool-output-chars 20000
# Keep the beginning instead of the default end.
python3 copilot_export.py --max-tool-output-chars 20000 --truncate-direction beginning

# Split long Markdown sessions; choose one size limit.
python3 copilot_export.py --split-long-sessions --max-session-md-bytes 800000
python3 copilot_export.py --split-long-sessions --max-session-md-mb 1

# Preview a run without writing its bundle.
python3 copilot_export.py --dry-run

# Select a profile, database, or output location.
python3 copilot_export.py --source /path/to/Code/User
# A database path also scans its containing directory for related stores.
python3 copilot_export.py --db "/path/to/Code/User/workspaceStorage/<workspace-hash>/state.vscdb"
python3 copilot_export.py --output ./my_copilot_export
```

Each run gets a new `<UTC timestamp>_<label>` directory under `--output`; numeric suffixes distinguish runs with the same timestamp and label. Markdown and JSON are written per session; JSONL writes one `history.jsonl` stream per run. `--jsonl-compact` changes the JSONL record layout.

### Default behavior

| Setting | Default |
|---|---|
| Formats | Markdown and per-session JSON |
| Content categories | All categories |
| Reasoning | Readable stored reasoning is included; serialized reasoning with no readable text is excluded |
| Turns | All available turns |
| Truncation | Disabled |
| Omission notices | Enabled for excluded content |
| Output | New timestamped bundle under `./copilot_export` |

These are export defaults, not a byte-for-byte raw copy of editor storage. The tool reads supported stores, normalizes and merges recognized records, and applies the serialized-reasoning exclusion above. `--label` only names the bundle; `--include-raw-reasoning` includes serialized reasoning, but does not bypass other reading or normalization behavior. This is the most inclusive supported export mode, not a raw database dump:

```bash
python3 copilot_export.py --label full-with-serialized-reasoning --include-raw-reasoning
```

Progress is automatic in an interactive terminal. Use `--progress` to force it when output is redirected, `--no-progress` to disable it, or `--quiet` to suppress progress and normal status output.

## Input discovery and platform scope

The exporter reads:

- `state.vscdb` entries used by Copilot chat history and indexes.
- Copilot `agentSessionData/<session-id>/session.db` files for stored assistant response parts and tool results.
- JSONL session files in `chatSessions`, `emptyWindowChatSessions`, and `transferredChatSessions`.
- `workspace.json` manifests to resolve workspace paths when available.

Automatic discovery checks known user-data locations for VS Code, Cursor, Windsurf, VSCodium, and code-server, including the adjacent `agentSessionData` directory. Detection is based on profile paths, not on whether GitHub Copilot is installed. The exporter reads Copilot-compatible data only; it does not export Windsurf's native Cascade conversations. The Windows Windsurf path has a unit test but has not yet been validated against a real Windows installation. macOS live discovery is also not yet validated on a real installation.

Some stores contain prompt history separately from assistant sessions and do not provide a direct session identifier for that history. The exporter deduplicates identical prompt sequences within a workspace, first pairs a history when exactly one unmatched agent session has the same number of turns, then pairs the last history and session if exactly one of each remains. This is a best-effort heuristic, not a universal session match; ambiguity or missing history can leave assistant messages without recovered prompts. Prompts already present in a chat-session record are read directly and do not depend on this pairing.

SQLite databases are read-only. A live lock may trigger a temporary snapshot including WAL/SHM sidecars. Explicit directories passed with `--source` are recursively scanned for supported stores. The exporter is intended to recover persisted, readable chat data, not to repair corrupt databases or perform forensic recovery.

## Content selection

Responses are retained internally as typed parts, then filtered before output:

- `prompts`: user requests.
- `answers`: rendered assistant responses.
- `reasoning`: stored `thinking` content, when VS Code persisted it.
- `tool-calls`: serialized tool invocations and their available input.
- `tool-results`: explicit tool results, when stored separately by VS Code.
- `edits`: serialized text edits and diffs.
- `references`: standalone references and code-block references. Inline references inside assistant prose are kept in place regardless of this selector.
- `tool-events`: ancillary lifecycle/status events such as progress updates, warnings, errors, confirmations, server startup, and unrecognized event kinds. These are not the tool's returned output and can add noise.

Assistant answers and standalone tool results are separate categories. Selecting `answers` does not include tool results; select `tool-results` to include them. Some VS Code tool events store readable summaries in `pastTenseMessage` and file/range hits in `resultDetails`.

The `--conversation` preset selects prompts, answers, and readable stored reasoning while omitting standalone tool calls/results, edits, references, and other events. Inline references inside assistant prose remain in place regardless of the `references` selector; Markdown renders them as readable labels rather than full paths or URIs.

Excluded tool parts are grouped into inline omission markers by default. `--no-omission-markers` removes those in-message notices; it does not change which content categories are selected, and omission details remain in metadata. Turn-gap notices are unaffected. Omission records use stable message/part indices rather than Markdown line numbers.

Content, turn, and truncation filters are applied before writing, so they affect all selected formats. `--jsonl-compact` changes only the JSONL record layout.

Serialized reasoning without readable text is excluded by default; `--include-raw-reasoning` includes it. When omission markers are enabled and reasoning is selected, these exclusions are labeled `serialized_reasoning omitted`. `--max-reasoning-chars` and `--max-tool-output-chars` truncate matching content; `--truncate-direction beginning` keeps the start instead. Truncated structured tool data is emitted as text.

`--turns` accepts positive, one-based turns and ranges, and cannot be combined with `--last-turns`. Turn filtering preserves original turn numbers and places a notice where excluded turns create a gap. `--last-turns N` selects the latest N turns per session.

## Output layouts

Each run gets a timestamped bundle. Markdown and JSON are organized by workspace and session; JSONL is written as one run-wide stream:

```text
copilot_export/
  20261105T145718Z_prompts/
    metadata.json
    2026-11-04__latest-workspace-edit__project-name__short-id/
      2026-11-04__session-title.md
      2026-11-04__session-title.json
    history.jsonl
```

The workspace folder name uses its latest session date, a readable workspace name, and a short ID. Session filenames use the session date and title.

Markdown files contain the conversation and session metadata in YAML frontmatter. Session JSON contains the session and its messages. When reported usage is available, it is included in Markdown frontmatter and JSON session data.

JSONL contains typed content-part records with session, turn, role, timestamp, and content, plus usage records when available. `--jsonl-compact` stores shared session metadata in one header per session. JSONL can feed a downstream RAG ingestion pipeline; chunking, embeddings, and prompt/answer dataset preparation are outside this exporter's scope.

`--split-long-sessions` splits Markdown between complete messages. Set the limit with either `--max-session-md-bytes` (default: 1,000,000) or `--max-session-md-mb`; a message larger than the limit remains intact.

## Run metadata and exit codes

Each bundle's `metadata.json` records its run ID and label, sources, formats, filters, output files, content statistics, and reported usage totals and coverage. Usage values come from the source; no token counts or costs are estimated. Repeating an export creates a separate bundle. Timestamps use UTC ISO 8601 format.

- `0`: successful export or dry run.
- `1`: source discovery, database read, or output write failure.
- `2`: valid sources were found but no chat records matched the selected filters.

## Verification and roadmap

```bash
python3 -m unittest discover -v
```

Tests use synthetic fixtures; they verify parser and discovery behavior but do not replace validation against real editor installations.

- [x] Hybrid SQLite and JSONL collection.
- [x] Isolated, timestamped bundles for Markdown, JSON, and structured JSONL exports.
- [x] Turn, content, reasoning, tool-output, and truncation controls with truthful metadata.
- [x] Readable workspace/session names with a short collision-safe identifier.
- [x] Isolated per-run bundles, compact JSONL, progress reporting, and message-safe Markdown splitting.
- [x] Auto-discover the Windows Windsurf user-data profile for Copilot-compatible storage.
- [ ] Validate Copilot export from a real Windows Windsurf installation; native Cascade conversations are out of scope.
- [ ] Validate discovery and live reads on a real macOS VS Code installation.
- [ ] Add anonymized synthetic fixtures for known storage variants, including ambiguous/missing prompt histories and inline-reference shapes.
- [ ] Investigate `chat-session-resources` and whether its contents and links to exported tool calls are useful to recover.
- [ ] Investigate whether `transcripts/` or `file_edits` can provide additional stored transcripts or file changes; these sources are not currently read.
- [ ] Investigate persisted Copilot memories and determine whether they are distinct, relevant conversation data or external context.
- [ ] Evaluate opt-in Copilot debug-log diagnostics for session-specific troubleshooting, including privacy and noise trade-offs.

## License and affiliation

This project is released under the MIT License; see [LICENSE](./LICENSE). It is an independent project and is not affiliated with or endorsed by Microsoft, GitHub, Visual Studio Code, or GitHub Copilot.
