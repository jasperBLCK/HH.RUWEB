import json
import re
from typing import Any, Optional

import httpx

from app.config import settings
from app.models import Profile, Vacancy

SCORE_PROMPT = """Ты помогаешь соискателю отбирать вакансии на hh.ru.
Оцени, насколько вакансия подходит кандидату, числом от 0 до 100, и дай одно короткое предложение с обоснованием.
Учитывай пожелания кандидата, его навыки и реальный опыт: не завышай оценку там, где требуется опыт сильно выше.

Профиль кандидата:
{profile}

Пожелания:
{wishes}

Вакансия:
Название: {name}
Компания: {employer}
Условия: {conditions}
Описание: {description}

Ответь строго JSON: {{"score": <int>, "reason": "<строка>"}}"""

LETTER_PROMPT = """Ты пишешь сопроводительное письмо на hh.ru от лица кандидата.
Правила:
- на русском, 1300-1900 символов, без markdown и без списков;
- обращение «Здравствуйте!», в конце контакты кандидата;
- привязывайся к конкретным задачам и стеку вакансии, упоминай компанию;
- опирайся только на реальный опыт и навыки из профиля, ничего не выдумывай;
- незнакомые технологии упоминай как готовность быстро освоить, а не как опыт;
- живой человеческий тон, без канцелярита и без пафоса.

Профиль кандидата:
{profile}

Пожелания:
{wishes}

Вакансия:
Название: {name}
Компания: {employer}
Условия: {conditions}
Описание: {description}

Верни только текст письма."""


def enabled() -> bool:
    return bool(settings.llm_api_key)


def _chat(prompt: str, max_tokens: int = 900) -> str:
    resp = httpx.post(
        f"{settings.llm_base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={
            "model": settings.llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.6,
            "max_tokens": max_tokens,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _profile_text(profile: Profile) -> str:
    return "\n".join(
        [
            f"Имя: {profile.full_name}",
            f"Навыки: {profile.skills}",
            f"Резюме: {profile.resume_text}",
            f"Минимальная зарплата: {profile.salary_min or 'не указана'}",
            f"Контакты и ссылки: {profile.links}",
        ]
    )


def _conditions(vacancy: Vacancy) -> str:
    parts = [vacancy.schedule, vacancy.employment, vacancy.experience, vacancy.area, vacancy.salary]
    return ", ".join(p for p in parts if p)


def _skills(profile: Profile) -> list[str]:
    return [s.strip() for s in re.split(r"[,\n]", profile.skills) if s.strip()]


def _match_skills(profile: Profile, vacancy: Vacancy) -> tuple[list[str], list[str]]:
    """Навыки кандидата, которые встречаются в тексте вакансии, и остальные."""
    haystack = f"{vacancy.name} {vacancy.description} {vacancy.key_skills}".lower()
    matched, rest = [], []
    for skill in _skills(profile):
        (matched if skill.lower() in haystack else rest).append(skill)
    return matched, rest


def keyword_score(profile: Profile, vacancy: Vacancy) -> tuple[int, str]:
    """Оценка без LLM: пересечение навыков кандидата с текстом вакансии."""
    skills = [s.lower() for s in _skills(profile)]
    haystack = f"{vacancy.name} {vacancy.description} {vacancy.key_skills}".lower()
    hits = [s for s in skills if s in haystack]
    score = min(100, int(len(hits) / max(len(skills), 1) * 100) + (10 if "junior" in haystack else 0))
    reason = "Совпадения по навыкам: " + (", ".join(hits[:8]) if hits else "нет")
    return score, reason


def score_vacancy(profile: Profile, vacancy: Vacancy) -> tuple[int, str]:
    if not enabled():
        return keyword_score(profile, vacancy)
    prompt = SCORE_PROMPT.format(
        profile=_profile_text(profile),
        wishes=profile.wishes,
        name=vacancy.name,
        employer=vacancy.employer,
        conditions=_conditions(vacancy),
        description=vacancy.description[:4000],
    )
    raw = _chat(prompt, max_tokens=300)
    data = _parse_json(raw)
    if data is None:
        return keyword_score(profile, vacancy)
    return int(data.get("score", 0)), str(data.get("reason", ""))[:400]


def _parse_json(raw: str) -> Optional[dict[str, Any]]:
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _vacancy_tasks(vacancy: Vacancy, limit: int = 3) -> list[str]:
    """Короткие содержательные фразы из описания вакансии для привязки письма."""
    lines = [line.strip(" -•—\t") for line in vacancy.description.splitlines()]
    picked = []
    for line in lines:
        if 25 <= len(line) <= 160 and not line.endswith(":"):
            picked.append(line.rstrip(".;"))
        if len(picked) == limit:
            break
    return picked


def fallback_letter(profile: Profile, vacancy: Vacancy) -> str:
    """Письмо без LLM: привязка к стеку и задачам вакансии."""
    matched, rest = _match_skills(profile, vacancy)
    company = vacancy.employer or "вашей команде"
    tasks = _vacancy_tasks(vacancy)

    blocks = [
        f"Здравствуйте! Меня зовут {profile.full_name or 'кандидат'}. "
        f"Откликаюсь на вакансию «{vacancy.name}»"
        + (f" в компании {vacancy.employer}." if vacancy.employer else ".")
    ]

    if matched:
        blocks.append(
            f"В описании вижу знакомый стек: {', '.join(matched[:8])} — с этим работаю "
            f"в своих проектах и на практике, поэтому смогу включиться без долгой раскачки."
        )
    if tasks:
        blocks.append(
            "Из задач особенно откликается: "
            + "; ".join(t.lower() for t in tasks)
            + ". Готов брать такие задачи на себя и доводить до результата."
        )
    if profile.resume_text.strip():
        blocks.append(profile.resume_text.strip())
    if rest:
        blocks.append(
            f"Дополнительно использую: {', '.join(rest[:8])}. "
            "То, чего пока не знаю, быстро осваиваю — привык разбираться в чужом коде и документации."
        )
    if profile.wishes.strip():
        blocks.append(f"По формату: {profile.wishes.strip()}")
    blocks.append(
        f"Буду рад обсудить задачи {company} и показать, как работаю. "
        + (f"Мои контакты и проекты: {profile.links}" if profile.links else "")
    )
    return "\n\n".join(b.strip() for b in blocks if b.strip())


def generate_letter(profile: Profile, vacancy: Vacancy) -> str:
    if not enabled():
        return fallback_letter(profile, vacancy)
    prompt = LETTER_PROMPT.format(
        profile=_profile_text(profile),
        wishes=profile.wishes,
        name=vacancy.name,
        employer=vacancy.employer,
        conditions=_conditions(vacancy),
        description=vacancy.description[:4000],
    )
    return _chat(prompt, max_tokens=1200)
