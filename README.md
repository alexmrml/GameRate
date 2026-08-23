# GameRate

Internal game intelligence service foundation built with FastAPI, Jinja2, SQLAlchemy, PostgreSQL,
Alembic, and a separate worker process.

## Local start

```bash
cp .env.example .env
docker compose up --build -d
docker compose exec web gamerate create-user admin
```

Open <http://localhost:8000/login> (or whichever `WEB_PORT` you set). The password prompt does not echo input or put it into shell
history. Run lint and tests locally with `pip install -e ".[dev]"`, `ruff check .`, and `pytest`.

Outside Compose, start the two processes independently with `uvicorn app.main:app` and
`python -m app.worker`; both read the same environment configuration and PostgreSQL database.

## Production deployment

The Ubuntu 24.04, rootless Docker, zrok and GitHub Actions deployment procedure is documented in
[`deploy/README.md`](deploy/README.md). Production uses a separate Compose file; the local
development workflow above is unchanged.

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

The catalogue at `/games` is paged 50 games at a time; the search, platform and sort filters stay
attached to the page links, and changing a filter returns to the first page.

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
and `/games/{id}` uses them to list similar games with the reason for each match. Summaries are
written in Russian whatever language the reviews are in; the reviews themselves are stored and
shown untranslated.

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
| `GEMINI_REQUEST_TIMEOUT_SECONDS` | Deadline for one model request (600s); a request that hits it is not retried, the run moves on |
| `AI_ENABLED` | Turn enrichment off without removing the key |
| `AI_MIN_REVIEWS` | Reviews an audience needs before it is summarised |
| `AI_MAX_GAMES_PER_RUN` | Upper bound on games enriched per run |
| `AI_REFRESH_MIN_NEW_REVIEWS` / `AI_REFRESH_MIN_GROWTH` | Both thresholds a refresh must clear |
| `AI_MIN_REFRESH_INTERVAL_HOURS` | Quiet period after a summary is generated |

The knobs in the lower half of that table can also be changed at runtime on `/settings`, where
each shows its environment default, any override, and the value in force.

## YouTube let's-play analysis

The same processing run also fills a separate let's-play perspective for games that do not yet
have a useful result. Games that critics have reviewed are worked through first, so a catalogue
full of brand-new indie releases does not spend every run on titles nobody has filmed.

Discovery makes one YouTube search per game, ordered by view count and served by yt-dlp, then
hydrates the results through the Data API's `videos.list`. It rejects everything that is not one
creator playing this game and talking about it: anything under eight minutes, Shorts, videos
YouTube does not file under Gaming, trailers, reviews, guides, collectible runs, compilations,
cutscene movies, mod footage and videos advertising no commentary. The most-viewed survivor wins.
A playthrough of a demo counts — for a game released last week it is often the only let's-play
that exists.

Searching through yt-dlp means the number of games discovered per day is no longer capped: the
Data API's search endpoint allows only about 100 calls a day, while the metadata endpoint it
still uses costs 1 of 10 000 units per 50 videos. A search takes about two seconds and needs no
cookies or account.

Empty searches and provider errors are persisted, so an hourly run does not repeat them
immediately; candidates are cached so a silent or unavailable video can advance to the next
result without another search.

The analysis itself reads the video's **subtitles**, not the video. yt-dlp reads the player
metadata, selects the caption track and fetches its signed JSON through its own network layer,
without downloading any video or audio. Machine-translated caption tracks are ignored, so the transcript
is always in the language actually spoken.

Rather than taking a fixed last-15-minutes slice, the app scans backwards from the end and keeps
the latest fragment that still contains speech: credits, menus and a silent outro push the window
earlier, a creator who talks to the last second is analysed at the very end. That text goes to
`gemma-4-31b-it` as an ordinary text request, which is fast and cheap enough to analyse several
games per run.

If a video publishes no usable subtitles at all — a "no commentary" walkthrough, for instance —
the app falls back to sending the video itself to a multimodal Gemini model. The same fallback
catches a video whose subtitles keep failing to download for technical reasons, so a broken
caption read never denies a game an analysis outright. That call is slow and quota-hungry, so it
is capped at one per run; the subtitle path is not.

Results are written in Russian regardless of the video's language, while the quoted evidence stays
in the creator's own words. The structured response keeps an overall impression, liked/disliked
points and a verbatim quote for each finding. A result is shown as useful only when its overall
quote is found in the transcript; otherwise the source gets a retryable status and the next run
tries the next candidate.

YouTube has its own models and failure state, so quota, search, unavailable-video and Gemini
errors cannot fail Metacritic collection or ordinary review/tag enrichment. The `/settings` page
can change every knob below the key rows without exposing either provider key. It also maintains
an optional yt-dlp proxy pool: raw URLs are accepted one at a time and only server-side masks are
rendered afterwards. Environment and UI entries are combined; one random proxy is kept for all
yt-dlp work belonging to a single game.

| Variable | Meaning |
| --- | --- |
| `GOOGLE_CLOUD_API_KEY` | YouTube Data API key, used for video metadata; environment-only |
| `YOUTUBE_ANALYSIS_ENABLED` | Enable/disable the YouTube phase |
| `YOUTUBE_ANALYSIS_MODEL` | Model reading the subtitle fragment (`gemma-4-31b-it`) |
| `YOUTUBE_VIDEO_FALLBACK_MODEL` | Multimodal model used only when subtitles are missing |
| `YOUTUBE_ANALYSIS_FRAGMENT_MINUTES` | Length of the analyzed fragment near the end |
| `YOUTUBE_TRANSCRIPT_MIN_WORDS_PER_MINUTE` | Speech rate below which a fragment counts as silence |
| `YOUTUBE_ANALYSIS_MAX_GAMES_PER_RUN` | Backlog cap per processing run (default 5) |
| `YOUTUBE_MAX_VIDEO_FALLBACKS_PER_RUN` | Multimodal video calls per run (default 1) |
| `YOUTUBE_SEARCH_MAX_RESULTS` | Results read from the single search page (50 costs no more than 5) |
| `YOUTUBE_RETRY_INTERVAL_HOURS` | Delay after provider/source failures |
| `YOUTUBE_NO_RESULT_REFRESH_DAYS` | When an empty search may be repeated |
| `YOUTUBE_PROXIES` | Comma/newline-separated yt-dlp proxy URLs (`http(s)`, `socks4`, `socks5`); environment entries are read-only in the UI |

Proxy URLs use `protocol://user:pass@ip:port`. Percent-encode commas inside credentials as `%2C`.
Do not commit real proxy URLs: credentials added in the web interface are stored in PostgreSQL and
are never returned to the browser after submission.
