# Changelog

Notable product changes, reconstructed from the available development history. Dates describe development milestones, not versioned releases.

## 2026-11

### Added

- Read-only `--check [--json]`, `--list-sessions [--json]`, and `--capabilities [--json]` commands with stable schemas for launchers and automation.
- Session-level selection with `--sessions`, `--last`, `--since`, `--filter`, and `--order`.
- Export Copilot-compatible local chat history to Markdown, per-session JSON, or JSONL, with run metadata and separate timestamped output bundles.
- Read and combine supported SQLite and JSONL sources, including agent-session data and separately stored prompt history.
- Select content categories and turns; use a conversation preset; and control reasoning, tool output, truncation, and long-session splitting.
- Include omission notices for excluded content and optionally preserve serialized reasoning.
- Export tool calls and results, inline references, stored edit-related content when available, and source-reported token usage.
- Add progress reporting, dry-run support, compact JSONL, and collision-safe names for simultaneous or same-timestamp runs.

### Changed

- The default output directory is now `./export` instead of `./copilot_export`.
- Session records retain their discovery source file for listings and metadata.
- Normalize records from supported stores into a common conversation representation before filtering and writing.
- Include readable reasoning by default while excluding serialized reasoning unless explicitly requested.
- Leave content untruncated unless a limit is specified; the default truncation direction is the end of the content.

### Fixed

- Improve prompt-to-turn association, reference-path normalization, and handling of empty or omitted content across supported storage variants.
- Keep Markdown output readable by rendering inline references as labels and omitting empty code fences.
