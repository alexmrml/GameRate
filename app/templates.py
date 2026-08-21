from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import settings

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals["app_name"] = settings.app_name
templates.env.globals["app_timezone"] = settings.app_timezone
