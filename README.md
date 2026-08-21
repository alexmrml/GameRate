# GameRate

Internal game intelligence service foundation built with FastAPI, Jinja2, SQLAlchemy, PostgreSQL,
Alembic, and a separate worker process.

## Local start

```bash
cp .env.example .env
docker compose up --build -d
docker compose exec web gamerate create-user admin
```

Open <http://localhost:8000/login>. The password prompt does not echo input or put it into shell
history. Run lint and tests locally with `pip install -e ".[dev]"`, `ruff check .`, and `pytest`.

Outside Compose, start the two processes independently with `uvicorn app.main:app` and
`python -m app.worker`; both read the same environment configuration and PostgreSQL database.

## Metacritic crawler

The worker collects games from Metacritic over plain HTTP. Each calendar day is one cycle:

- the first run of the day takes the 20 games of the **New Releases** carousel;
- every later run that day continues through the **browse** listing, picking up the next games
  that have not been handled yet today;
- a run processes at most `CRAWL_BATCH_SIZE` games, and no game is processed twice in one day.

A run starts automatically every `SCHEDULE_INTERVAL_MINUTES` (60 by default), and the worker
queues one as soon as it starts. **Start processing** on `/activity` queues the same job by hand;
both use the same code path. The Activity page streams status, progress, current game and the run
message while a batch is in flight, and `/games` fills up as games are saved.

For each game the crawler stores title, cover, developer, publisher, description, release date,
genres, video link when Metacritic has one, and every platform with its own Metascore and
Userscore, plus the critic and user reviews that later feed AI summaries. A game that fails is
recorded on the run and skipped; the rest of the batch still completes.

Crawl progress lives in PostgreSQL, so restarting the worker resumes the same day where it
stopped and marks the interrupted run as failed instead of blocking the queue.

Useful settings (see `.env.example` for the defaults):

| Variable | Meaning |
| --- | --- |
| `CRAWL_BATCH_SIZE` | Games per run |
| `SCHEDULE_INTERVAL_MINUTES` | Gap between automatic runs |
| `CRAWL_REQUEST_DELAY_SECONDS` | Minimum spacing between Metacritic requests |
| `CRAWL_CRITIC_REVIEW_PAGES` | Critic review pages fetched per game (10 reviews each) |
| `CRAWL_USER_REVIEWS_PER_PLATFORM` | User reviews stored per platform |
| `CRAWL_MAX_PLATFORMS` | Platforms queried per game |
| `RUN_STALE_SECONDS` | When a silent run counts as abandoned |

## AI summaries, tags and similar games

Every run ends with an enrichment pass over the games it just collected. For each game Gemini
reads the collected reviews and answers three things per audience — what people liked, what they
disliked, and an overall summary — with critics and players kept strictly apart. A game with no
reviews, or with fewer than `AI_MIN_REVIEWS` for an audience, stays empty: the page says so
instead of showing invented text. The same pass derives tags from the description and metadata,
and `/games/{id}` uses them to list similar games with the reason for each match.

Set `GEMINI_API_KEY` in `.env` to switch it on; without a key the crawler runs exactly as before
and the run records why enrichment was skipped. The default model is `gemma-4-31b-it`, because
free keys allow only about five requests a minute on the `gemini-*-flash` models and the hourly
crawler needs more headroom. On a paid key, set `ai.model` on `/settings` (for example
`gemini-3.5-flash`) — no restart required.

Summaries are not regenerated for free: a repeat pass over unchanged reviews makes no model call
at all, and an existing summary is only rebuilt once enough new reviews arrive, measured both in
absolute count and in relative growth.

| Variable | Meaning |
| --- | --- |
| `GEMINI_API_KEY` | Gemini key; environment-only, never editable from `/settings` |
| `GEMINI_MODEL` | Default model (`ai.model` on `/settings` overrides it) |
| `GEMINI_REQUESTS_PER_MINUTE` | Pacing so a batch stays inside the key's quota |
| `AI_ENABLED` | Turn enrichment off without removing the key |
| `AI_MIN_REVIEWS` | Reviews an audience needs before it is summarised |
| `AI_MAX_GAMES_PER_RUN` | Upper bound on games enriched per run |
| `AI_REFRESH_MIN_NEW_REVIEWS` / `AI_REFRESH_MIN_GROWTH` | Both thresholds a refresh must clear |
| `AI_MIN_REFRESH_INTERVAL_HOURS` | Quiet period after a summary is generated |

The knobs in the lower half of that table can also be changed at runtime on `/settings`, where
each shows its environment default, any override, and the value in force.

## YouTube let's-play analysis

The same processing run also fills a separate let's-play perspective for games that do not yet
have a useful result. Discovery makes one popularity-ordered YouTube Data API search per game,
hydrates all returned metadata in one `videos.list`, rejects trailers, reviews, guides, demos,
soundtracks, cutscenes, Shorts, no-commentary videos and similar false positives, then chooses
the remaining video with the most views. Empty searches and provider errors are persisted, so an
hourly run does not repeat them immediately; candidates are cached so a silent or unavailable
video can advance to the next result without another search.

Gemini receives the public YouTube URL directly — the application never calls the captions API
and never downloads video or audio. Only the configured number of minutes at the end of the video
is sent (15 by default, or the whole available video when shorter). The structured response keeps
a speech transcript, overall impression, liked/disliked points and a verbatim speech quote for
each finding. A result is shown as useful only when its overall quote is found in the transcript;
otherwise the source gets a retryable `no_useful_commentary` status.

YouTube has its own model and failure state, so quota, search, unavailable-video and Gemini errors
cannot fail Metacritic collection or ordinary review/tag enrichment. Video calls can take several
minutes, which is why the default is one game per run. The `/settings` page can change the feature
toggle, model, fragment length and per-run cap without exposing either provider key.

| Variable | Meaning |
| --- | --- |
| `GOOGLE_CLOUD_API_KEY` | YouTube Data API key; environment-only |
| `YOUTUBE_ANALYSIS_ENABLED` | Enable/disable the YouTube phase |
| `YOUTUBE_ANALYSIS_MODEL` | Multimodal Gemini model (`gemini-3.5-flash` by default) |
| `YOUTUBE_ANALYSIS_FRAGMENT_MINUTES` | Minutes analyzed from the end of the video |
| `YOUTUBE_ANALYSIS_MAX_GAMES_PER_RUN` | Backlog cap per processing run (default 1) |
| `YOUTUBE_SEARCH_MAX_RESULTS` | Candidates fetched by the single search request |
| `YOUTUBE_RETRY_INTERVAL_HOURS` | Delay after provider/source failures |
| `YOUTUBE_NO_RESULT_REFRESH_DAYS` | When an empty search may be repeated |
