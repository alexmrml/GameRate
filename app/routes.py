import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload

from app.auth import (
    AuthContext,
    clear_session_cookie,
    create_session,
    find_auth_context,
    require_auth,
    require_csrf,
    set_session_cookie,
)
from app.config import settings
from app.db import SessionLocal, get_db
from app.models import (
    AppSetting,
    Audience,
    Game,
    GamePlatform,
    GameReview,
    Platform,
    ProcessingRun,
    User,
    UserSession,
    WorkerHeartbeat,
)
from app.security import verify_password
from app.services.app_settings import describe_settings
from app.services.runs import enqueue_manual_run
from app.services.similarity import lead_platform, rank_similar
from app.templates import templates
from app.time import utc_now

router = APIRouter()


def page_context(request: Request, auth: AuthContext, **extra: object) -> dict[str, object]:
    return {
        "request": request,
        "current_user": auth.user,
        "csrf_token": auth.session.csrf_token,
        **extra,
    }


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, object]:
    db.execute(text("SELECT 1"))
    cutoff = utc_now() - timedelta(seconds=settings.worker_stale_seconds)
    active_workers = db.scalar(
        select(func.count())
        .select_from(WorkerHeartbeat)
        .where(WorkerHeartbeat.last_seen_at >= cutoff)
    )
    return {"status": "ok", "database": "ok", "active_workers": active_workers or 0}


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request, next: str = "/games", db: Session = Depends(get_db)
) -> HTMLResponse:
    if find_auth_context(request, db):
        return RedirectResponse("/games", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"next": next, "error": None},
    )


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/games",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    user = db.scalar(select(User).where(User.username == username.strip()))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"next": next, "error": "Invalid username or password"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    _, raw_token = create_session(db, user)
    destination = next if next.startswith("/") and not next.startswith("//") else "/games"
    response = RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, raw_token)
    return response


@router.post("/logout")
def logout(
    csrf_token: Annotated[str, Form()],
    auth: AuthContext = Depends(require_auth),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    require_csrf(csrf_token, auth)
    session = db.get(UserSession, auth.session.id)
    if session:
        db.delete(session)
        db.commit()
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    clear_session_cookie(response)
    return response


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/games", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/games", response_class=HTMLResponse)
def games_page(
    request: Request,
    q: str = "",
    platform: str = "",
    sort: str = Query(default="released", pattern="^(title|released|metascore|userscore)$"),
    auth: AuthContext = Depends(require_auth),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    platform_rows = db.scalars(select(Platform).order_by(Platform.name)).all()
    stmt = select(Game).options(selectinload(Game.platforms).selectinload(GamePlatform.platform))
    if q:
        stmt = stmt.where(Game.title.ilike(f"%{q.strip()}%"))
    if platform:
        stmt = stmt.where(Game.platforms.any(GamePlatform.platform.has(Platform.slug == platform)))
    games = list(db.scalars(stmt).unique())

    # One score represents a game everywhere: the lead platform's, chosen in app.services
    # .similarity. Sorting therefore happens on the same value the row displays, which SQL
    # aggregates cannot express, so the ordering is applied here.
    rows = [
        {
            "game": game,
            "lead": lead_platform(game),
            "release_date": game.release_date,
        }
        for game in games
    ]
    if sort == "title":
        rows.sort(key=lambda row: row["game"].title.casefold())
    elif sort == "released":
        rows.sort(
            key=lambda row: (row["release_date"] is not None, row["release_date"] or date.min),
            reverse=True,
        )
    elif sort in {"metascore", "userscore"}:

        def score_of(row: dict[str, object]) -> tuple[int, float]:
            lead = row["lead"]
            value = None if lead is None else getattr(lead, sort)
            # Unrated games sort last on their own rank; no placeholder score is invented.
            return (0, 0.0) if value is None else (1, float(value))

        rows.sort(key=lambda row: row["game"].title.casefold())
        rows.sort(key=score_of, reverse=True)
    else:
        rows.sort(key=lambda row: row["game"].updated_at, reverse=True)

    return templates.TemplateResponse(
        request=request,
        name="games.html",
        context=page_context(
            request,
            auth,
            rows=rows,
            platforms=platform_rows,
            filters={"q": q, "platform": platform, "sort": sort},
        ),
    )


@router.get("/games/{game_id}", response_class=HTMLResponse)
def game_detail(
    request: Request,
    game_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    game = db.scalar(
        select(Game)
        .where(Game.id == game_id)
        .options(
            selectinload(Game.platforms).selectinload(GamePlatform.platform),
            selectinload(Game.genres),
            selectinload(Game.tags),
            selectinload(Game.review_summaries),
            selectinload(Game.youtube_analyses),
        )
    )
    if game is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Game not found")

    def latest_reviews(audience: Audience) -> list[GameReview]:
        return list(
            db.scalars(
                select(GameReview)
                .where(GameReview.game_id == game.id, GameReview.audience == audience)
                .options(selectinload(GameReview.platform))
                .order_by(GameReview.review_date.desc().nullslast(), GameReview.collected_at.desc())
                .limit(10)
            )
        )

    review_counts = dict(
        db.execute(
            select(GameReview.audience, func.count())
            .where(GameReview.game_id == game.id)
            .group_by(GameReview.audience)
        ).all()
    )
    summaries = {item.audience: item for item in game.review_summaries if item.platform_id is None}

    # Similarity is plain weighted arithmetic over the loaded catalogue, so the neighbours
    # are always current instead of a stored snapshot that ages as games are added.
    candidates = list(
        db.scalars(
            select(Game).options(
                selectinload(Game.tags),
                selectinload(Game.genres),
                selectinload(Game.platforms).selectinload(GamePlatform.platform),
            )
        ).unique()
    )
    similar = rank_similar(game, candidates, limit=settings.similar_games_limit)

    return templates.TemplateResponse(
        request=request,
        name="game_detail.html",
        context=page_context(
            request,
            auth,
            game=game,
            lead=lead_platform(game),
            critic_reviews=latest_reviews(Audience.CRITICS),
            user_reviews=latest_reviews(Audience.USERS),
            critic_review_count=review_counts.get(Audience.CRITICS, 0),
            user_review_count=review_counts.get(Audience.USERS, 0),
            critic_summary=summaries.get(Audience.CRITICS),
            user_summary=summaries.get(Audience.USERS),
            similar_games=similar,
            tags_by_facet=_tags_by_facet(game),
        ),
    )


def _tags_by_facet(game: Game) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = {}
    for tag in sorted(game.tags, key=lambda item: (item.facet or "", item.name)):
        grouped.setdefault(tag.facet or "descriptors", []).append(tag)
    return grouped


@router.get("/activity", response_class=HTMLResponse)
def activity_page(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    runs = db.scalars(
        select(ProcessingRun)
        .options(selectinload(ProcessingRun.current_game))
        .order_by(ProcessingRun.created_at.desc())
        .limit(100)
    ).all()
    workers = db.scalars(
        select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_seen_at.desc())
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="activity.html",
        context=page_context(request, auth, runs=runs, workers=workers),
    )


@router.post("/activity/runs")
def create_run(
    csrf_token: Annotated[str, Form()],
    auth: AuthContext = Depends(require_auth),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    require_csrf(csrf_token, auth)
    enqueue_manual_run(db, auth.user.id)
    return RedirectResponse("/activity", status_code=status.HTTP_303_SEE_OTHER)


def run_snapshot() -> dict[str, object]:
    with SessionLocal() as db:
        runs = db.scalars(
            select(ProcessingRun)
            .options(selectinload(ProcessingRun.current_game))
            .order_by(ProcessingRun.created_at.desc())
            .limit(20)
        ).all()
        workers = db.scalars(
            select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_seen_at.desc())
        ).all()
        return {
            "runs": [
                {
                    "id": str(run.id),
                    "status": run.status.value,
                    "message": run.message,
                    "worker_id": run.worker_id,
                    "progress_current": run.progress_current,
                    "progress_total": run.progress_total,
                    "current_game": run.current_game.title if run.current_game else None,
                    "updated_at": run.updated_at.isoformat(),
                }
                for run in runs
            ],
            "workers": [
                {
                    "worker_id": worker.worker_id,
                    "last_seen_at": worker.last_seen_at.isoformat(),
                    "current_run_id": str(worker.current_run_id) if worker.current_run_id else None,
                }
                for worker in workers
            ],
        }


@router.get("/activity/events")
async def activity_events(
    request: Request,
    _auth: AuthContext = Depends(require_auth),
) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        while not await request.is_disconnected():
            yield f"event: activity\ndata: {json.dumps(run_snapshot())}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    setting_rows = db.scalars(select(AppSetting).order_by(AppSetting.key)).all()
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context=page_context(request, auth, settings=setting_rows, tunables=describe_settings(db)),
    )


@router.post("/settings")
def update_setting(
    key: Annotated[str, Form()],
    value: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
    auth: AuthContext = Depends(require_auth),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    require_csrf(csrf_token, auth)
    normalized_key = key.strip()
    if not normalized_key or len(normalized_key) > 120:
        raise HTTPException(status_code=422, detail="Invalid setting key")
    try:
        parsed_value = json.loads(value)
    except json.JSONDecodeError:
        parsed_value = value
    row = db.get(AppSetting, normalized_key)
    now = utc_now()
    if row is None:
        row = AppSetting(key=normalized_key, value=parsed_value, updated_at=now)
        db.add(row)
    else:
        row.value = parsed_value
        row.updated_at = now
    row.updated_by_id = auth.user.id
    db.commit()
    return RedirectResponse("/settings", status_code=status.HTTP_303_SEE_OTHER)
