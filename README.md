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
