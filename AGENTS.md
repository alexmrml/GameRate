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
  `no_candidate`, provider/quota failures, unavailable sources, `no_transcript` and
  `no_useful_commentary`, plus the complete candidate cache and attempted video IDs. Do not
  replace this with in-memory retry state or search again while `next_retry_at` is ahead.
- **The search is yt-dlp; the metadata is `videos.list`.** Each does the half only it can.
  `search.list` has its own daily allowance of roughly 100 calls, which was the real ceiling
  on how many games a day could be discovered — it was exhausted during one afternoon of
  filter work while `videos.list` kept answering 200. yt-dlp has no such ceiling, so the
  per-run search cap is gone. But yt-dlp's flat search returns no `categoryId` and a
  description truncated to ~150 characters, and the filter needs both, so hydration stays on
  `videos.list`: 1 unit of 10 000 per 50 videos, which no realistic backlog can exhaust.
- Discovery makes one search and one `videos.list` per 50 results, asking for the full 50 —
  one search page costs the same at any depth. It does not use `videoDuration`. Candidates
  are filtered after hydration, then the eligible candidate with the highest `viewCount`
  wins. Eligibility is a yes/no test, never a score: the most-viewed survivor is the answer,
  so a "quality" ranking would have nowhere to live. A failed/silent source advances through
  cached candidates before another search is allowed.
- The search URL carries `sp=CAM%253D`, YouTube's sort-by-view-count filter. This is not a
  detail: `ytsearch:` orders by relevance, and a relevance-ordered page does not contain the
  most-viewed suitable video. Measured across the sample, relevance order picked a 922-view
  Creepshow video over a 24 743-view one and a 193k Mortal Shell II over a 325k. Sorted
  order picked the most-viewed eligible video for 22 of 30 games against the Data API's 12.
  Sorting does cost some depth on obscure games (Suncroft returned 17 rows on relevance and
  0 when sorted); that was measured and changed no outcome, since none of those rows was
  eligible. Do not swap the sort back without re-measuring both.
- The Data API is not kept as a search fallback. It could only serve ~100 searches a day,
  which cannot sustain the feature on its own, and a second search path would need its own
  error mapping for no gain: a failed yt-dlp search is already a retryable `youtube_error`.
- **YouTube loses recall as a quoted OR-chain grows.** Measured against one game: 1 branch
  returned 22-25 results, 3 returned 25, 4 returned 7, and the 7 branches this project used
  to send returned **1**. That single fact caused most `no_candidate` results — the search
  starved before any filter ran. `MAX_QUERY_BRANCHES` caps the query at three; do not add a
  fourth term "for coverage", it removes coverage. The searched name is `search_title()`:
  the game's name minus bracketed suffixes and edition labels.
- Rejection rules, in the order they fire, each earning its place against real results:
  `shorts`, `too_short` (under 8 minutes — this alone removes trailers, teasers and clipped
  highlights without a keyword), `not_gaming` (YouTube's own category 20, which is what
  separates the game *Gallipoli* from the battle documentary and *Superposition* from the
  circuit-theory lecture; it rides along in the snippet already fetched), `different_game`,
  the `_REJECT_TERMS` list, then `no_commentary`.
- The filter used to require a positive "gameplay" signal from *every* candidate. That is
  what silently deleted small channels, whose titles often say only the game's name, and it
  is now applied **only** to ambiguous names. `demo`, `beta` and `early access` were also
  dropped from the reject list: for a game released last week, a demo playthrough is often
  the only let's-play with live commentary that exists. Publisher B-roll (`gameplay demo`,
  `official gameplay`, `gameplay reveal`) is still rejected. So is a finale — `ending` was
  removed from the list, since the end of a playthrough is where opinions actually land.
- `_matches_game` compares **contiguous phrases**, not token coverage. The old 80%-of-tokens
  rule let "Dragon's Dogma 2 ... chef-d'oeuvre" satisfy the game *Chef's Dogma*. Variants
  cover dotted acronyms (`S.T.A.L.K.E.R. 2` ↔ `STALKER 2`) and roman numerals
  (`Mortal Shell II` ↔ `Mortal Shell 2`), and a trailing sequel marker is checked so
  *Mortal Shell* does not claim every *Mortal Shell II* video.
- A name of one or two ordinary words identifies nothing by itself, so those and only those
  must additionally: head one of the first two title segments, not sit behind a chapter
  counter (`JUSANT - Chapter 1 - Daymark` is Jusant), carry a playthrough signal, and name
  the game in the description too. **Known limitation:** when such a name is also a level,
  map or quest in a bigger game and that game's video says the word everywhere — *Gallipoli*
  as a Battlefield 1 map, *Superposition* as a Marvel Contest of Champions quest — no
  text-only rule separates them. `topicCategories` was checked and is genre-level only, so
  it cannot help. Two of thirty sampled games still pick the wrong game this way; prefer
  leaving it than adding heuristics that cost the twenty-five that work.
- The chapter-counter test is symmetric: a counter on *either* side of an ambiguous name
  marks it as a level label, because "JUSANT - Chapter 1 - Daymark" and "Jusant - DAYMARK
  (Chapter 1)" are the same video named two ways. It only applies to a name that does not
  lead the title — in "Slayblade - Part 1" the counter is this game's episode number, which
  is exactly what a let's-play looks like.
- **The main path is subtitles, not video.** `app/collectors/transcript.py` uses yt-dlp with
  `download=False` to read the player response, then fetches the `json3` timed-text track
  over plain HTTP. No video or audio stream is ever downloaded and no temporary file is
  written. Sending the video to Gemini was quota-bound to roughly one game per run; reading
  captions costs about 5 seconds and one ordinary text call, which is why the per-run cap is
  now 5 games and the one-game limit survives only on the fallback.
- Caption track choice is a correctness rule, not a preference. YouTube publishes machine
  *translations* of the automatic captions under every language code, marked by `tlang=` in
  the URL; a translation of a transcription cannot support a verbatim quote. Only original
  tracks are eligible, manual subtitles beat automatic ones, and `info["language"]` decides
  which original wins.
- `select_tail_window` scans backwards from the end in one-fifth-window steps and takes the
  *latest* window that still clears `youtube.min_words_per_minute`. That bar (15 wpm) is an
  anomaly filter, not a ranking: measured let's-play tails run 40-100 wpm, with Russian
  speech at the low end, so only real silence — credits, menus, outro music, an idle camera —
  pushes the window earlier. The search stops three windows back from the end, so "near the
  end" stays meaningful for a nine-hour stream. Verified live: a 9.7 h stream tails at 89
  wpm, a 5.2 h Russian one at 51 wpm, and a "no commentary" walkthrough publishes no
  captions at all.
- `analyze_letsplay_transcript` is a plain text call, so `gemma-4-31b-it` serves the main
  path. Its schema deliberately has no `speech_transcript` field: the transcript is an
  input, and the evidence check is only meaningful against text the model never rewrote.
  The stored transcript is always the fetched one.
- Gemini Video (`youtube.video_fallback_model`, `gemini-3.5-flash`) is now the fallback and
  only runs for a source whose *captions* are missing — never for one yt-dlp could not read
  at all. `youtube.max_video_fallbacks_per_run` (1) is what keeps the old quota limit off the
  main path. Served Gemma has no audio track and cannot take the fallback's place.
- Reading subtitles makes no model call, so one game may walk up to `MAX_TRANSCRIPT_ATTEMPTS`
  cached candidates looking for one with captions before the fallback budget is spent. One
  game still yields at most one summary, from one video.
- yt-dlp breaking on a video says nothing about whether Gemini can watch it, so a technical
  caption failure must not lock a game out of the fallback for good. After
  `TRANSCRIPT_ERRORS_BEFORE_FALLBACK` (2) consecutive failures on the same source it becomes
  fallback-eligible; one plain retry comes first, because a single timeout is far likelier
  to be a blip than a permanent obstacle and the fallback budget is the scarce one. The
  streak resets when a read succeeds or the candidate changes.
- Output is speech-grounded. The overall conclusion and each liked/disliked point carry
  verbatim speech evidence; only evidence found in the stored transcript is accepted.
  Tutorial steps, useful items and momentary frustration are not an overall game opinion. A
  source without a supported overall view is `no_useful_commentary`, not a fabricated summary.
- The YouTube phase works the backlog **reviewed games first** (`enrich_youtube_games` sorts
  by critic review count, then by whether the run collected the game). It is a sort, not a
  filter: an unreviewed game still comes up once the reviewed backlog is clear. Without it a
  catalogue that is ~85% brand-new indie releases spends every run on titles nobody has
  published a let's-play of.
- There is no per-run search cap any more, and `youtube.max_searches_per_run` and the
  `search_budget` outcome are gone with it. The cap existed only to ration `search.list`.
  What now bounds a run is `youtube.max_games_per_run`, which is about work per run rather
  than a provider allowance.
- Measured from the container: 30 consecutive searches, 0 failures, ~2.0 s per game
  including hydration. No cookies, no account, no per-video extraction — `extract_flat`
  reads the search page only. Rate limiting never appeared at this volume; a run does five.
- `GOOGLE_CLOUD_API_KEY` and `GEMINI_API_KEY` are environment-only. YouTube feature/model/
  fragment/density/batch tunables use `app/services/app_settings.py`; no YouTube failure may
  fail the crawl or stop ordinary Gemini enrichment.

## Generated language

- Every sentence the models generate — review summaries, verdicts, tags' prose, let's-play
  findings — is written in **Russian**, whatever language the source is in. The rule lives in
  `RUSSIAN_OUTPUT_RULE` and is appended to the review and both YouTube system prompts, so a
  new prompt gets it by concatenation rather than by remembering to restate it.
- Sources are never translated. Collected reviews are stored and displayed verbatim, and the
  quote fields (`speech_evidence`, `overall_opinion_evidence`) stay in the creator's own
  language — that is what makes the transcript containment check work at all.
- Prompt examples were rewritten in Russian along with the rule. A model steered by English
  examples drifts back into English regardless of the instruction.
- The interface chrome is still English on purpose; translating it is a separate stage.

## Testing

Tests never touch the live site, YouTube or Gemini. `tests/fixtures/metacritic/` holds trimmed
captures of real pages: the genuine `__NUXT_DATA__` payload with unrelated components removed,
verified to parse identically to the page it came from. Gemini is exercised through a fake SDK
object (`tests/test_gemini_client.py`) and a stub client (`tests/conftest.py`). yt-dlp never runs
in tests: `TranscriptClient` takes `extract_info` and `fetch_url` callables, so the suite feeds it
recorded caption structures instead. `tests/conftest.py` pins collection, AI and YouTube settings
through environment variables, so a developer's `.env` — which does hold real keys — cannot change
test outcomes or trigger a live call.

## Current state and next work

Auth, catalogue and detail pages, activity queue with SSE progress, settings, health, worker
heartbeat, Compose, CI, the Metacritic pipeline, Gemini review/tag enrichment, weighted similar
games, and YouTube let's-play discovery/subtitle analysis are in place. Model output is Russian.

Discovery was measured on a 30-game sample drawn from the live catalogue; the numbers and the
rejection-reason breakdown are in the YouTube section above. Next work, in the order it matters:

- **Interface translation.** Only generated text is Russian today; every template label, status
  string, table header and empty-state sentence in `app/templates/` is still English, and the
  `status.replace('_', ' ')` rendering on the detail page prints raw status slugs at the user.
  Decide whether statuses get a display map or the UI stops showing them before translating.
- **Discovery accuracy for one-word names.** Rewritten and measured on a 30-game sample from
  the live catalogue, re-run through the shipped code in the container: 27 select a video
  (was 20), 0 videos are eligible for more than one game, and the surviving errors are the
  *Gallipoli* / *Superposition* class described above. Anything further needs a game-entity
  vocabulary, not another keyword.
- **Prompt-version backlog.** `PROMPT_VERSION` and `YOUTUBE_PROMPT_VERSION` were both bumped, so
  every stored summary is stale and regenerates at `ai.max_games_per_run` (20) per run; a ~190
  game catalogue takes about ten runs to come back. That is by design — do not bulk-edit rows.
- Final product design.

Keep new integrations behind service-layer boundaries and store raw provider identifiers/status
for idempotency.
