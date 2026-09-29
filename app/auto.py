"""Автопилот: сам ищет вакансии, оценивает, пишет письма и откликается."""

import asyncio
import time
from datetime import datetime
from typing import Optional

import httpx
from sqlmodel import select

from app import hh, service
from app.db import get_session
from app.models import Vacancy

PAUSE_BETWEEN_APPLIES = 45  # секунд, чтобы hh не считал отклики массовой рассылкой

_task: Optional[asyncio.Task] = None
_running_cycle = False


def is_cycle_running() -> bool:
    return _running_cycle


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def _loop() -> None:
    while True:
        profile = service.load_profile()
        if profile.auto_enabled:
            try:
                await run_cycle()
            except Exception as exc:  # цикл не должен умирать из-за одной ошибки
                service.log(f"Автопилот: ошибка цикла — {exc}")
        await asyncio.sleep(max(profile.auto_interval_minutes, 5) * 60)


async def run_cycle() -> dict[str, int]:
    """Один проход: поиск → оценка → письмо → отклик. Возвращает счётчики."""
    global _running_cycle
    if _running_cycle:
        return {"skipped": 1}
    _running_cycle = True
    try:
        return await asyncio.to_thread(_cycle)
    finally:
        _running_cycle = False


def _cycle() -> dict[str, int]:
    profile = service.load_profile()
    stats = {"found": 0, "scored": 0, "applied": 0, "failed": 0}

    if hh.current_token() is None:
        service.log("Автопилот: нет токена hh — подключи доступ в профиле")
        return stats
    queries = service.search_queries(profile)
    if not queries:
        service.log("Автопилот: не заданы запросы поиска — заполни «Что искать» в профиле")
        return stats

    for query in queries:
        try:
            added, _ = service.run_search(profile, query)
            stats["found"] += added
            service.log(f"Поиск «{query}»: новых вакансий {added}")
        except httpx.HTTPError as exc:
            service.log(f"Поиск «{query}» не удался: {exc}")

    with get_session() as session:
        pending = session.exec(select(Vacancy).where(Vacancy.status == "new", Vacancy.score == 0)).all()
        pending_ids = [v.id for v in pending]
    for vacancy_id in pending_ids:
        try:
            service.score(profile, vacancy_id)
            stats["scored"] += 1
        except Exception as exc:
            service.log(f"Оценка вакансии {vacancy_id} не удалась: {exc}")

    if not profile.resume_id:
        service.log("Автопилот: не указан resume_id — отклики не отправляю")
        return stats

    budget = profile.auto_max_per_day - service.applied_today()
    if budget <= 0:
        service.log("Дневной лимит откликов исчерпан")
        return stats

    with get_session() as session:
        candidates = session.exec(
            select(Vacancy)
            .where(Vacancy.status == "new", Vacancy.score >= profile.auto_min_score)
            .order_by(Vacancy.score.desc())
        ).all()
        candidate_ids = [v.id for v in candidates]

    for vacancy_id in candidate_ids[:budget]:
        try:
            service.write_letter(profile, vacancy_id)
            ok, _ = service.apply(profile, vacancy_id, auto=True)
            stats["applied" if ok else "failed"] += 1
        except Exception as exc:
            stats["failed"] += 1
            service.log(f"Отклик на вакансию {vacancy_id} не удался: {exc}")
        time.sleep(PAUSE_BETWEEN_APPLIES)

    service.log(
        f"Цикл завершён {datetime.utcnow():%H:%M} UTC: найдено {stats['found']}, "
        f"оценено {stats['scored']}, откликов {stats['applied']}, ошибок {stats['failed']}"
    )
    return stats
