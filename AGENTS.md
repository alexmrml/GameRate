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
  `(game_id, external_key)`. It is the only input the review summaries are allowed to use.

## Gemini enrichment

- `app/collectors/gemini.py` uses the official `google-genai` SDK. Every call is constrained by a
  pydantic `response_schema` and the reply is re-validated locally, so no caller ever parses free
  text. `GEMINI_API_KEY` is environment-only and must never become a `/settings` value.
- The schema is chosen from the audiences that actually have reviews (critics only, users only,
  or both), which is what keeps the two apart and stops the model from inventing the missing one.
  Below `ai.min_reviews` an audience is left empty; "no data" is a state, never a generated
  paragraph, and a game's description is never turned into an opinion.
- Errors are split by what the caller should do: `GeminiUnavailable` (bad key, no access, unknown
  model) disables Gemini for the rest of the run, `GeminiTemporaryError` (quota, 5xx) skips one
  game and lets the next try, `GeminiInvalidResponse` retries then reports. Nothing propagates
  into the crawl: a run whose collection succeeded stays SUCCEEDED.
- Free keys allow about **5 requests per minute** on `gemini-*-flash` but far more on
  `gemma-4-31b-it`, which is why Gemma is the default for an hourly crawler; its output quality
  was comparable in side-by-side checks. `GEMINI_REQUESTS_PER_MINUTE` paces calls, and a 429 with
  a `retryDelay` is honoured instead of guessed. Change `ai.model` from `/settings` on a paid key.
- `PROMPT_VERSION` in the collector is part of `review_summaries.input_digest`
  (`"<version>:<sha256 of review keys>"`). Bumping it after a prompt change makes every stored
  summary stale and regenerates it on the next run — do that instead of editing rows by hand.
- Refresh policy (`summary_needs_refresh`): generate when there is no summary; regenerate only
  when the review set grew by at least `ai.refresh_min_new_reviews` **and** by at least
  `ai.refresh_min_growth` relative to the previous count, and not within
  `ai.min_refresh_interval_hours`. Tags are regenerated only when the description or the hard
  metadata behind them changes (`Game.ai_tags_digest`). An unchanged hourly pass therefore costs
  zero model calls.
- Tunables live in `app/services/app_settings.py`: environment default, optional database
  override editable from `/settings`, no restart needed. Add new knobs there rather than reading
  `settings.*` directly in the worker.

## Similar games

- `app/services/similarity.py` is plain weighted arithmetic — no embeddings, no similarity SQL.
  Feature sets are compared with `|A∩B| / sqrt(|A|·|B|)`, which does not punish a richly tagged
  game for having extra tags the way Jaccard does.
- A component counts only when both games have it, and the comparable weights form the
  denominator, so missing data is not scored as difference. The result is then multiplied by
  `sqrt(share of weight compared)`: without that a barely-known game sharing one genre outscored
  a genuinely similar game with a full feature set. `Comparison` keeps `raw`, `confidence` and
  `score` separate so both rules stay testable.
- Weights (relative, not percentages): mechanics .28, genres .16, setting .12, structure .10,
  style .10, Metacritic peer listing .08, descriptors .06, developer .06, mood .05, score .04,
  ESRB .03, publisher .02, platforms .02, release year .02.
- Tags come from a controlled vocabulary in the collector (`FACET_VOCABULARY`) plus up to five
  free `descriptors`. A fixed vocabulary is what makes tags comparable between games; values
  outside it are dropped rather than stored.
- Two extra signals are taken from the game page the crawler already fetches: the ESRB rating,
  and `related_slugs` from Metacritic's own genre-peer carousel. Both are sparse, which the
  available-weight rule handles.
- `lead_platform()` — most critic reviews, then highest Metascore — defines the one score that
  represents a game. The catalogue list, its sorting, the similarity score band and the Gemini
  context all use it, so the number a user sees is the number everything else reasons about.
  Unrated games show "not rated" and sort last; never substitute 0.

## YouTube analysis

- `youtube_analyses` has one durable state row per game. It stores success as well as
  `no_candidate`, provider/quota failures, unavailable sources and `no_useful_commentary`, plus
  the complete candidate cache and attempted video IDs. Do not replace this with in-memory retry
  state or search again while `next_retry_at` is in the future.
- Discovery makes exactly one popularity-ordered `search.list` call and one batched `videos.list`
  call per search. It does not use `videoDuration`; Shorts and irrelevant formats are rejected
  after metadata hydration, then the suitable candidate with the highest `viewCount` wins. A
  failed/silent source advances through cached candidates before another search is allowed.
- Gemini receives a public YouTube URL with `VideoMetadata.start_offset/end_offset`; captions API,
  video downloads and audio downloads are intentionally absent. The configured tail fragment is
  15 minutes by default, or the whole video when shorter.
- Video opinion output is speech-grounded. The overall conclusion and each liked/disliked point
  carry verbatim speech evidence; only evidence found in the stored transcript is accepted.
  Tutorial steps, useful items and momentary frustration are not an overall game opinion. A
  source without a supported overall view is `no_useful_commentary`, not a fabricated summary.
- Video analysis has a separate model/session/status from review/tag enrichment. The default is
  `gemini-3.5-flash`, one video per run because live 15-minute calls took minutes. Served Gemma 4
  31B has no audio track and cannot replace Gemini for creator-speech analysis.
- `GOOGLE_CLOUD_API_KEY` and `GEMINI_API_KEY` are environment-only. YouTube feature/model/fragment/
  batch tunables use `app/services/app_settings.py`; no YouTube failure may fail the crawl or stop
  ordinary Gemini enrichment.

## Testing

Tests never touch the live site or Gemini. `tests/fixtures/metacritic/` holds trimmed captures of
real pages: the genuine `__NUXT_DATA__` payload with unrelated components removed, verified to
parse identically to the page it came from. Gemini is exercised through a fake SDK object
(`tests/test_gemini_client.py`) and a stub client (`tests/conftest.py`). `tests/conftest.py` pins
collection and AI settings through environment variables, so a developer's `.env` — which does
hold a real key — cannot change test outcomes or trigger a live call.

## Current state and next work

Auth, catalogue and detail pages, activity queue with SSE progress, settings, health, worker
heartbeat, Compose, CI, the Metacritic pipeline, Gemini review/tag enrichment, weighted similar
games, and YouTube let's-play discovery/speech analysis are in place. Next work is the final
product design. Keep new integrations behind service-layer boundaries and store raw provider
identifiers/status for idempotency.
