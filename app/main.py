from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routes import router


def create_app() -> FastAPI:
    application = FastAPI(title=settings.app_name)
    application.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )
    application.include_router(router)
    return application


app = create_app()
