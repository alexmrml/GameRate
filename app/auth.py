from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User, UserSession
from app.security import new_token, token_digest
from app.time import utc_now


@dataclass(slots=True)
class AuthContext:
    user: User
    session: UserSession


def create_session(db: Session, user: User) -> tuple[UserSession, str]:
    now = utc_now()
    raw_token = new_token()
    user_session = UserSession(
        token_hash=token_digest(raw_token),
        csrf_token=new_token(),
        user_id=user.id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=settings.session_ttl_hours),
    )
    db.add(user_session)
    db.commit()
    db.refresh(user_session)
    return user_session, raw_token


def set_session_cookie(response: RedirectResponse, raw_token: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        raw_token,
        max_age=settings.session_ttl_hours * 60 * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: RedirectResponse) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/")


def find_auth_context(request: Request, db: Session) -> AuthContext | None:
    raw_token = request.cookies.get(settings.session_cookie_name)
    if not raw_token:
        return None

    now = utc_now()
    session = db.scalar(
        select(UserSession).where(
            UserSession.token_hash == token_digest(raw_token),
            UserSession.expires_at > now,
        )
    )
    if session is None or not session.user.is_active:
        return None
    session.last_seen_at = now
    db.commit()
    return AuthContext(user=session.user, session=session)


def require_auth(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    context = find_auth_context(request, db)
    if context is None:
        next_url = quote(str(request.url.path), safe="/")
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": f"/login?next={next_url}"},
        )
    return context


def require_csrf(candidate: str, context: AuthContext) -> None:
    from app.security import tokens_match

    if not tokens_match(candidate, context.session.csrf_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
