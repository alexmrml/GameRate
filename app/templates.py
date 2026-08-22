import re
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import settings

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals["app_name"] = settings.app_name
templates.env.globals["app_timezone"] = settings.app_timezone

RUN_STATUS_LABELS = {
    "queued": "В очереди",
    "running": "Выполняется",
    "succeeded": "Завершён",
    "failed": "Ошибка",
    "cancelled": "Отменён",
}
RUN_TRIGGER_LABELS = {
    "manual": "Вручную",
    "daily": "Ежедневно",
    "scheduled": "По расписанию",
}
YOUTUBE_STATUS_LABELS = {
    "pending": "Ожидает обработки",
    "success": "Готово",
    "no_candidate": "Подходящее видео не найдено",
    "youtube_error": "Ошибка YouTube",
    "youtube_unavailable": "YouTube недоступен",
    "youtube_quota_exhausted": "Квота YouTube исчерпана",
    "video_unavailable": "Видео недоступно",
    "no_transcript": "Субтитров нет",
    "transcript_error": "Ошибка получения субтитров",
    "no_useful_commentary": "Содержательного мнения не найдено",
    "gemini_error": "Ошибка анализа",
    "gemini_unavailable": "Модель анализа недоступна",
    "gemini_quota_exhausted": "Квота анализа исчерпана",
    "internal_error": "Внутренняя ошибка",
}
YOUTUBE_REASON_LABELS = {
    "The selected fragment contained no useful creator opinion": (
        "В выбранном фрагменте не нашлось содержательного мнения автора"
    ),
    "No suitable let's-play candidate was found": "Подходящий летсплей не найден",
}


def format_run_message(message: str | None) -> str:
    """Translate the worker's stable progress vocabulary without altering stored logs."""
    if not message:
        return "—"
    main, *notes = message.split(" · ")
    exact = {
        "Waiting for worker": "Ожидает воркер",
        "Claimed by worker": "Взят воркером",
        "Selecting games from Metacritic": "Выбор игр в Metacritic",
        "No unprocessed games left for today": "На сегодня необработанных игр не осталось",
        "Interrupted; requeue to continue today's crawl": (
            "Запуск прерван; добавьте новый, чтобы продолжить сегодняшний обход"
        ),
    }
    main = exact.get(main, main)
    patterns = (
        (r"^Processing (\d+) games from the (.+) stage$", r"Обработка игр: \1 · этап \2"),
        (r"^Collecting (.+) \((\d+)/(\d+)\)$", r"Сбор \1 (\2/\3)"),
        (r"^Saved (.+) \((\d+)/(\d+)\)$", r"Сохранено: \1 (\2/\3)"),
        (r"^Failed (.+) \((\d+)/(\d+)\): (.+)$", r"Ошибка \1 (\2/\3): \4"),
        (r"^Analyzing reviews for (\d+) games$", r"Анализ отзывов · игр: \1"),
        (r"^Analyzing YouTube let's-plays for (\d+) games$", r"Анализ YouTube-летсплеев · игр: \1"),
        (r"^YouTube analysis for (.+) \((\d+)/(\d+)\)$", r"YouTube-анализ: \1 (\2/\3)"),
        (r"^Analyzing (.+) \((\d+)/(\d+)\)$", r"AI-анализ: \1 (\2/\3)"),
        (r"^Processed (\d+) of (\d+) games, (\d+) failed$", r"Обработано \1 из \2 игр; ошибок: \3"),
        (r"^Processed (\d+) games$", r"Обработано игр: \1"),
        (r"^All (\d+) games failed$", r"Все игры завершились с ошибкой: \1"),
        (r"^Discovery failed: (.+)$", r"Ошибка обнаружения: \1"),
    )
    for pattern, replacement in patterns:
        translated = re.sub(pattern, replacement, main)
        if translated != main:
            main = translated
            break

    translated_notes: list[str] = []
    for note in notes:
        if note.startswith("AI: "):
            note = note.replace(" enriched", " обогащено")
            note = re.sub(r"(\d+) AI failures?", r"ошибок AI: \1", note)
            note = note.replace("AI stopped", "AI остановлен")
        elif note.startswith("YouTube: "):
            note = note.replace(" analyzed", " проанализировано")
            note = note.replace(" without a candidate", " без подходящего видео")
            note = note.replace(" failed", " ошибок")
            note = note.replace(" via video fallback", " через резервный видеоанализ")
        translated_notes.append(note)
    return " · ".join((main, *translated_notes))


templates.env.globals["run_status_labels"] = RUN_STATUS_LABELS
templates.env.globals["run_trigger_labels"] = RUN_TRIGGER_LABELS
templates.env.globals["youtube_status_labels"] = YOUTUBE_STATUS_LABELS
templates.env.globals["youtube_reason_labels"] = YOUTUBE_REASON_LABELS
templates.env.globals["format_run_message"] = format_run_message
