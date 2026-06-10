# ADR 0001 — Single-tenant by design; `user_id` is forward-compat, not isolation

Status: Accepted (2026-06-10)

## Context

Every table carries a `user_id` column and queries filter on it, yet the value is
hard-coded `USER_ID = "default"` across the services and the auth token's subject
is the constant string `"user"` (`auth.py`). This looks like multi-tenancy but
provides none: there is exactly one user.

This was flagged in the code review (finding C3) as a "false sense of isolation" —
the `user_id` filters can be mistaken for a security boundary they do not enforce.

## Decision

The system is **intentionally single-user**. We are **not** ripping out the
`user_id` columns/filters: doing so would touch every table and query for a
low-severity cleanup, with real regression risk on a live deployment. Instead we
record the decision explicitly:

- `user_id` is **forward-compatibility scaffolding**, not an access-control
  boundary. Do not rely on a `WHERE user_id = ...` clause for isolation.
- Access control is the single shared password (cookie/JWT) plus the
  trusted-internal service token (see `auth.py`). There is no per-user authz.
- `USER_ID = "default"` is the canonical tenant id; new code should reuse the
  existing constant rather than re-deriving it.

## Consequences

- If real multi-tenancy is ever needed, this becomes a deliberate project:
  derive `user_id` from the authenticated identity, add per-user authz, and audit
  every query — the columns already exist to make that migration tractable.
- Reviewers should treat `user_id` filters as namespacing/partitioning, not
  security, and not assume cross-tenant protection that isn't there.
