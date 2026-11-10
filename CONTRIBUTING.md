# Contributing

Contributions that improve compatibility, correctness, and documentation are welcome.

## Preserve the architecture

Keep source discovery and reading, normalization, filtering, and output writing as distinct responsibilities. Handle storage-specific details in the reader layer, normalize them into the shared conversation model, and keep filters and writers independent of a particular storage layout. If a change needs to cross these boundaries, explain why.

Copilot and editor storage formats are external and may change. Base compatibility on observed data, support known variants explicitly, and avoid assuming that one schema or record shape is universal. Document compatibility limits rather than claiming coverage that has not been verified.

## Tests and documentation

Run the test suite before submitting changes:

```bash
python3 -m unittest discover -v
```

Use synthetic fixtures for database and transcript variations. Update tests for behavior changes and update the README when CLI options, defaults, supported inputs, or output formats change. Add user-visible changes to the changelog.

## Privacy and scope

Do not commit real conversations, database files, secrets, personal identifiers, or private machine paths. Keep fixtures and examples synthetic and anonymized. The exporter should read editor data without modifying it; recovery of damaged databases and forensic reconstruction are outside the project's scope.
