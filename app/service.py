"""Общие операции над вакансиями: используются и панелью, и автопилотом."""

import re
from datetime import datetime, timedelta

from sqlmodel import select

from app import hh, llm
from app.db import get_session
from app.models import LogEntry, Profile, Vacancy


def load_profile() -> Profile:
    with get_session() as session:
        profile = session.exec(select(Profile)).first()
        if profile is None:
            profile = Profile()
            session.add(profile)
            session.commit()
            session.refresh(profile)
        return profile


def log(message: str) -> None:
    with get_session() as session:
        session.add(LogEntry(message=message))
        session.commit()


def recent_logs(limit: int = 30) -> list[LogEntry]:
    with get_session() as session:
        return session.exec(select(LogEntry).order_by(LogEntry.id.desc()).limit(limit)).all()


def search_queries(profile: Profile) -> list[str]:
    """Запросы для hh: явный список из профиля, иначе первая строка инструкции."""
    queries = [q.strip() for q in profile.search_queries.splitlines() if q.strip()]
    if queries:
        return queries
    instruction = profile.search_instruction.strip()
    return [instruction.splitlines()[0]] if instruction else []


def is_excluded(profile: Profile, vacancy: Vacancy) -> str:
    """Стоп-слово, из-за которого вакансию не стоит брать, либо пустая строка."""
    words = [w.strip().lower() for w in re.split(r"[,\n]", profile.exclude_words) if w.strip()]
    haystack = f"{vacancy.name} {vacancy.employer} {vacancy.description}".lower()
    return next((w for w in words if w in haystack), "")


def upsert_vacancy(hh_id: str) -> bool:
    """Скачивает вакансию с hh и сохраняет локально. True — если она новая."""
    details = hh.vacancy_details(hh_id)
    salary = details.get("salary") or {}
    salary_text = ""
    if salary:
        salary_text = f"{salary.get('from') or ''}–{salary.get('to') or ''} {salary.get('currency') or ''}".strip("– ")
    with get_session() as session:
        vacancy = session.exec(select(Vacancy).where(Vacancy.hh_id == hh_id)).first()
        is_new = vacancy is None
        if vacancy is None:
            vacancy = Vacancy(hh_id=hh_id, name=details.get("name", ""))
        vacancy.name = details.get("name", "")
        vacancy.employer = (details.get("employer") or {}).get("name", "")
        vacancy.url = details.get("alternate_url", f"https://hh.ru/vacancy/{hh_id}")
        vacancy.salary = salary_text
        vacancy.schedule = (details.get("schedule") or {}).get("name", "")
        vacancy.employment = (details.get("employment") or {}).get("name", "")
        vacancy.experience = (details.get("experience") or {}).get("name", "")
        vacancy.area = (details.get("area") or {}).get("name", "")
        vacancy.description = hh.strip_html(details.get("description", ""))
        vacancy.key_skills = ", ".join(s["name"] for s in details.get("key_skills", []))
        session.add(vacancy)
        session.commit()
    return is_new


def run_search(profile: Profile, query: str, pages: int = 1) -> tuple[int, int]:
    added, updated = 0, 0
    for page in range(max(pages, 1)):
        items = hh.search_vacancies(
            query,
            page=page,
            remote_only=profile.remote_only,
            salary_min=profile.salary_min,
        )
        for item in items:
            if upsert_vacancy(item["id"]):
                added += 1
            else:
                updated += 1
    return added, updated


def score(profile: Profile, vacancy_id: int) -> tuple[int, str]:
    with get_session() as session:
        vacancy = session.get(Vacancy, vacancy_id)
        if vacancy is None:
            raise LookupError("vacancy not found")
        excluded = is_excluded(profile, vacancy)
        if excluded:
            value, reason = 0, f"Стоп-слово в вакансии: {excluded}"
        else:
            value, reason = llm.score_vacancy(profile, vacancy)
        vacancy.score = value
        vacancy.score_reason = reason
        session.add(vacancy)
        session.commit()
    return value, reason


def write_letter(profile: Profile, vacancy_id: int) -> str:
    with get_session() as session:
        vacancy = session.get(Vacancy, vacancy_id)
        if vacancy is None:
            raise LookupError("vacancy not found")
        vacancy.letter = llm.generate_letter(profile, vacancy)
        session.add(vacancy)
        session.commit()
        return vacancy.letter


def apply(profile: Profile, vacancy_id: int, auto: bool = False) -> tuple[bool, str]:
    with get_session() as session:
        vacancy = session.get(Vacancy, vacancy_id)
        if vacancy is None:
            raise LookupError("vacancy not found")
        letter = vacancy.letter or llm.generate_letter(profile, vacancy)
        ok, detail = hh.apply(vacancy.hh_id, profile.resume_id, letter)
        vacancy.letter = letter
        vacancy.status = "applied" if ok else "failed"
        vacancy.status_detail = detail
        vacancy.applied_at = datetime.utcnow() if ok else None
        vacancy.auto = auto
        session.add(vacancy)
        session.commit()
        name, employer = vacancy.name, vacancy.employer
    if auto:
        log(f"{'Отклик отправлен' if ok else 'Отклик не ушёл'}: {name} — {employer}. {detail}"[:400])
    return ok, detail


def applied_today() -> int:
    since = datetime.utcnow() - timedelta(days=1)
    with get_session() as session:
        rows = session.exec(select(Vacancy).where(Vacancy.status == "applied")).all()
    return sum(1 for v in rows if v.applied_at and v.applied_at >= since)
