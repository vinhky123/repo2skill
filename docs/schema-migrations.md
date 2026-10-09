# Ledger schema compatibility

P0 ledger documents use `schemaVersion` `1.0`. Each project, run, state, manifest, manifest set, and export journal is versioned. The index is disposable and is never authoritative.

Readers reject an unknown major schema version. Within the current major version, mutable documents preserve unknown additive object fields when rewritten. Those extra fields are not interpreted by the current program and do not participate in IDs or digests. Immutable run and manifest artifacts are not rewritten. Additive compatibility does not make arbitrary edits to ledger JSON safe.

P0 does not promise automatic migration between major versions. Before moving a repository state directory to a build with a different supported schema major, preserve a copy of `.repo2skill/` and consult the release notes and this guide. If the build refuses the schema, stop using mutating commands until an explicit migration path is documented; do not change `schemaVersion` by hand. No migration command or general rollback tool is currently specified.
