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
