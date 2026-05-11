# Refactor Progress

This file tracks implementation progress for `docs/refactor-plan.md`.

When later implementation errors are found and fixed, record them here with:

- date
- affected phase
- symptom
- root cause
- fix
- verification

## 2026-05-11 - Phase 0 Baseline and Constraints

Status: implemented.

Assumptions:

- Phase 0 should not change runtime behavior.
- Baseline artifacts should be useful before and after larger refactors.
- Smoke checks should use the existing running API and avoid new test
  dependencies.

Implemented:

- Added API route baseline: `docs/baseline/api-routes.md`.
- Added DB schema baseline: `docs/baseline/db-schema.md`.
- Added stdlib smoke test script: `scripts/refactor_smoke.py`.
- Added Make target: `make refactor-smoke`.

Verification scope:

- `GET /api/health`
- `POST /api/auth/login`
- `GET /api/auth/me`
- `GET /api/sources`
- `GET /api/kb/nodes?limit=1`
- `GET /api/briefing`
- `GET /api/drafts`

How to run:

```sh
AUTH_PASSWORD=... make refactor-smoke
```

If auth is intentionally unavailable, the unauthenticated subset can be run
with:

```sh
python scripts/refactor_smoke.py --skip-auth
```

Notes:

- `git status --short` worked in this workspace, so no Git `safe.directory`
  fix was needed here.
- Phase 0 records that `source_items`, `jobs`, and object-specific node tables
  do not exist yet.
- The baseline records that `knowledge_edges` currently lacks a uniqueness
  constraint, so duplicate cleanup is required before adding one.
- Live verification on this workspace:
  - `python scripts/refactor_smoke.py --skip-auth` passed for health, sources,
    KB nodes, and briefing.
  - Auth-only coverage was checked inside the API container with its existing
    `AUTH_PASSWORD`; login and `GET /api/drafts` passed.
  - The API container was initially unhealthy because a reload attempted DB
    connection while Postgres was starting (`CannotConnectNowError`). Restarting
    only the API container after Postgres was healthy fixed the environment.

Implementation fixes:

- Symptom: the first local smoke attempt raised a raw `ConnectionResetError`
  instead of printing a normal failure line.
- Root cause: `scripts/refactor_smoke.py` only caught `urllib.error.URLError`,
  but socket-level reset errors can surface as `OSError`.
- Fix: catch `OSError` around the smoke run and report it as `api connection`
  failure.
- Verification: `python -m py_compile scripts/refactor_smoke.py` passes.
