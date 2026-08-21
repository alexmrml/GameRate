# GameRate agent notes

## Stack and layout

Python 3.12+, FastAPI with server-rendered Jinja2 and plain CSS/JS, synchronous SQLAlchemy 2,
PostgreSQL, and Alembic. `app/routes.py` owns the initial HTTP surface, `app/services/` owns
transactional domain operations, `app/collectors/` owns read-only external readers with no
database access, and `app/worker.py` is a separately launched process from the same package.
Web and worker share configuration and models but no process memory.

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
- Database-backed settings are JSON values. Deployment configuration and secrets remain
  environment-owned and must not be editable from `/settings`.

## Metacritic collection

- Metacritic is a Nuxt SSR site. `app/collectors/nuxt.py` reads the `__NUXT_DATA__` devalue
  payload each page embeds — the API response the page was rendered from. Do not add CSS/DOM
  selectors; the markup is generated and unstable, the payload is not. Plain HTTP is sufficient
  and no browser automation is used or needed. `robots.txt` allows `/game/` and `/browse/`.
- Data locations, all verified against live pages: per-platform **Metascore** comes from the game
  page (`product.item.platforms[].criticScoreSummary`), but per-platform **Userscore** exists
  only on `/game/<slug>/user-reviews/?platform=<slug>`, so one request per platform is required.
  Critic reviews come from `/game/<slug>/critic-reviews/` (10 per page, each row carries its own
  platform). Cover art is `<base>/a/img/{bucketType}{bucketPath}`, which resolves unsigned.
- A pending userscore is served as `score: 0` with `sentiment: null`. It is stored as NULL:
  never persist it as a real 0.0 rating. Missing scores generally arrive as `null` already.
- Collector functions raise `MetacriticError`/`NuxtPayloadError` on bad responses. Never
  substitute empty results for a failed fetch.

## Crawl cycle and runs

- One calendar day is one cycle. The first run of a day takes the 20 games of the New Releases
  carousel; every later run that day walks `/browse/game/all/all/all-time/new/` (24 per page)
  from the stored cursor. A run handles at most `CRAWL_BATCH_SIZE` games.
- Traversal state lives in PostgreSQL (`daily_crawl_states.cursor`: stage, browse page, offset,
  failed slugs), never in worker memory, so a container restart resumes the same day. Games
  already handled today are `daily_processed_games` rows; slugs that failed before ever reaching
  the catalogue are the cursor's `failed_slugs`. Both are filtered out for the rest of the day.
- `app/services/pipeline.py:execute_run` is the only processing path; manual and scheduled runs
  differ solely by `ProcessingRun.trigger`. It commits after each game so `/activity` and its SSE
  stream show progress, current game, counters and message live.
- One failing game is recorded and skipped; a failing discovery step fails the whole run. A run
  is FAILED only when discovery failed or every planned game failed.
- The hourly schedule is derived from the runs table (`ensure_scheduled_run`), not from an
  in-process timer, and respects the single-RUNNING-row invariant. `recover_stale_runs` fails
  runs abandoned by a restarted worker (same `worker_id`) or stalled past `RUN_STALE_SECONDS`,
  and releases their crawl state so the next run can continue.
- A PostgreSQL partial unique index allows only one `processing_runs.status = RUNNING` row. Keep
  job claiming transactional and do not weaken this invariant when adding schedulers.
- `game_reviews` stores collected critic and user reviews verbatim, keyed by
  `(game_id, external_key)`, as the input for the not-yet-connected AI summaries. `ReviewSummary`
  stays empty until Gemini is wired in.

## Testing

Tests never touch the live site. `tests/fixtures/metacritic/` holds trimmed captures of real
pages: the genuine `__NUXT_DATA__` payload with unrelated components removed, verified to parse
identically to the page it came from. `tests/conftest.py` pins collection settings through
environment variables so a local `.env` cannot change test outcomes.

## Current state and next work

Auth, game browsing/detail pages, activity queue with SSE progress, settings, health, worker
heartbeat, Compose, CI, and the full Metacritic pipeline (discovery, per-platform scores, review
collection, hourly schedule, restart recovery) are in place. Next work is Gemini review
summarization over `game_reviews`, YouTube analysis, and final product design. Add each
integration behind service-layer boundaries and record raw provider identifiers for idempotency.
