---
status: complete
description: Add link dragging between groups
commit: c379c9c
---

# Link dragging between groups

Added a dedicated dotted pointer handle to bookmark rows. Users can drop onto visible group headers or contents, including nested, empty, and folded groups. Mouse and touch browser tests cover the shared pointer path; pen uses the same implementation. The existing move selector remains available for keyboard use.

The gesture reuses the existing move operation, source-membership semantics, request lifetime checks, and error handling. Escape, pointer cancellation, lost capture, rendering, and disposal clean up the gesture. Pending moves cannot be repeated from the same row. Existing destination memberships do not create duplicate links, and unrelated memberships remain intact.

Updated README usage guidance, committed generated browser modules, and adjusted the DOM child-order contract for the new handle. Backend and OpenAPI contracts are unchanged.

## Verification

- Targeted link/group drag tests: 19 passed.
- DOM helper tests: 8 passed.
- `just check`: artifact drift checks, backend/frontend import contracts, lint, formatting, and typing passed. The final full pytest run recorded 1,389 passes before the disposable PostgreSQL server became unavailable, producing 32 setup/teardown errors (including one teardown error on an otherwise passing test).
- Recovery with a fresh disposable PostgreSQL container: `uv run pytest --lf -q` passed all 32 affected tests. No unresolved test failures remain.
- Earlier test-only failures were corrected: the seed helper moved membership instead of adding one, folded links required unfolding before role assertions, and the DOM contract needed the new handle.
- `git diff --cached --check` passed before the implementation commit.

Execution followed the quick workflow inline. Implementation commit: `c379c9c`.
