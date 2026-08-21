# GameRate agent notes

## Stack and layout

Python 3.12+, FastAPI with server-rendered Jinja2 and plain CSS/JS, synchronous SQLAlchemy 2,
PostgreSQL, and Alembic. `app/routes.py` owns the initial HTTP surface, `app/services/` owns
transactional domain operations, and `app/worker.py` is a separately launched process from the
same package. Web and worker share configuration and models but no process memory.

## Decisions and constraints

- Authentication is intentionally local-only: no registration, roles, OAuth, or JWT. Session
  cookies contain a random opaque token; only its SHA-256 digest is stored. Passwords use the
  current `pwdlib` Argon2 recommendation. Authenticated mutations require a per-session CSRF
  token.
- `Game.source_key` is the canonical discovery identity. Prefer `metacritic:<stable external key>`;
  the fallback combines normalized title and release year. Discovery code must upsert by this key.
- All instants are timezone-aware and stored as UTC-compatible PostgreSQL `timestamptz`. Only the
  daily processing date is a calendar `date`, calculated through `app.time.app_today()` using
  `APP_TIMEZONE`.
- A PostgreSQL partial unique index allows only one `processing_runs.status = RUNNING` row. Keep
  job claiming transactional, refresh heartbeat during future long-running collectors, and do not
  weaken this database invariant when adding schedulers.
- The worker currently acknowledges jobs as a successful no-op. External network integrations
  and collection logic do not belong in route handlers.
- Database-backed settings are JSON values. Deployment configuration and secrets remain
  environment-owned and must not be editable from `/settings`.

## Current state and next work

The foundation includes auth, game browsing/detail pages, activity queue with SSE status,
settings, health, worker heartbeat, Compose, tests, linting, CI, and the initial schema. Next work
is the real discovery/refresh pipeline, then Metacritic ingestion, review summarization, YouTube
analysis, Gemini integration, retry/recovery policy, and final product design. Add each integration
behind service-layer boundaries and record raw provider identifiers needed for idempotency.
