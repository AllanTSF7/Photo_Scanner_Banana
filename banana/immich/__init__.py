"""Read-only Immich integration (explicitly authorized, on the condition it stays read-only - see CLAUDE.md).

Nothing here may upload, edit, stack, delete, or trigger a library scan in Immich. `client.ImmichClient` has no
put/patch/delete method at all, and its one POST path is checked against a hard allow-list before any request
is made. See `docs/specifications.md` for the full design.
"""
