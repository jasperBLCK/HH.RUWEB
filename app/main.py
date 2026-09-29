from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app import hh, llm
from app.config import settings
from app.db import get_session, init_db
from app.models import Profile, Vacancy

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="HH.RUWEB")


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    with get_session() as session:
        if session.exec(select(Profile)).first() is None:
            session.add(Profile())
            session.commit()


def load_profile() -> Profile:
    with get_session() as session:
        profile = session.exec(select(Profile)).first()
        if profile is None:
            profile = Profile()
            session.add(profile)
            session.commit()
            session.refresh(profile)
        return profile


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, status: str = "new") -> HTMLResponse:
    with get_session() as session:
        query = select(Vacancy).order_by(Vacancy.score.desc(), Vacancy.found_at.desc())
        if status != "all":
            query = query.where(Vacancy.status == status)
        vacancies = session.exec(query).all()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "vacancies": vacancies,
            "status": status,
            "profile": load_profile(),
            "authorized": hh.current_token() is not None,
            "llm_enabled": llm.enabled(),
        },
    )


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "profile.html",
        {
            "request": request,
            "profile": load_profile(),
            "authorized": hh.current_token() is not None,
            "authorize_url": hh.authorize_url() if settings.hh_client_id else "",
        },
    )


@app.post("/profile")
def save_profile(
    full_name: str = Form(""),
    resume_id: str = Form(""),
    resume_text: str = Form(""),
    skills: str = Form(""),
    wishes: str = Form(""),
    salary_min: int = Form(0),
    remote_only: bool = Form(False),
    links: str = Form(""),
) -> RedirectResponse:
    with get_session() as session:
        profile = session.exec(select(Profile)).first() or Profile()
        profile.full_name = full_name
        profile.resume_id = resume_id
        profile.resume_text = resume_text
        profile.skills = skills
        profile.wishes = wishes
        profile.salary_min = salary_min
        profile.remote_only = remote_only
        profile.links = links
        session.add(profile)
        session.commit()
    return RedirectResponse("/profile", status_code=303)


@app.post("/auth/token")
def auth_token(access_token: str = Form(...)) -> RedirectResponse:
    hh.save_manual_token(access_token.strip())
    return RedirectResponse("/profile", status_code=303)


@app.get("/auth/callback")
def auth_callback(code: str = "") -> RedirectResponse:
    if code:
        hh.exchange_code(code)
    return RedirectResponse("/profile", status_code=303)


@app.get("/api/resumes")
def api_resumes() -> JSONResponse:
    try:
        items = [{"id": r["id"], "title": r.get("title", "")} for r in hh.my_resumes()]
    except httpx.HTTPError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"items": items})


@app.post("/api/search")
def api_search(query: str = Form(...), pages: int = Form(1)) -> JSONResponse:
    profile = load_profile()
    added, updated = 0, 0
    try:
        for page in range(max(pages, 1)):
            items = hh.search_vacancies(
                query,
                page=page,
                remote_only=profile.remote_only,
                salary_min=profile.salary_min,
            )
            for item in items:
                if _upsert_vacancy(item["id"]):
                    added += 1
                else:
                    updated += 1
    except httpx.HTTPError as exc:
        return JSONResponse({"error": f"hh API: {exc}"}, status_code=502)
    return JSONResponse({"added": added, "updated": updated})


def _upsert_vacancy(hh_id: str) -> bool:
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


@app.post("/api/score/{vacancy_id}")
def api_score(vacancy_id: int) -> JSONResponse:
    profile = load_profile()
    with get_session() as session:
        vacancy = session.get(Vacancy, vacancy_id)
        if vacancy is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        score, reason = llm.score_vacancy(profile, vacancy)
        vacancy.score = score
        vacancy.score_reason = reason
        session.add(vacancy)
        session.commit()
    return JSONResponse({"score": score, "reason": reason})


@app.post("/api/letter/{vacancy_id}")
def api_letter(vacancy_id: int) -> JSONResponse:
    profile = load_profile()
    with get_session() as session:
        vacancy = session.get(Vacancy, vacancy_id)
        if vacancy is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        vacancy.letter = llm.generate_letter(profile, vacancy)
        session.add(vacancy)
        session.commit()
        letter = vacancy.letter
    return JSONResponse({"letter": letter})


@app.post("/api/letter/{vacancy_id}/save")
def api_letter_save(vacancy_id: int, letter: str = Form("")) -> JSONResponse:
    with get_session() as session:
        vacancy = session.get(Vacancy, vacancy_id)
        if vacancy is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        vacancy.letter = letter
        session.add(vacancy)
        session.commit()
    return JSONResponse({"ok": True})


@app.post("/api/apply/{vacancy_id}")
def api_apply(vacancy_id: int) -> JSONResponse:
    profile = load_profile()
    if not profile.resume_id:
        return JSONResponse({"error": "не указан resume_id в профиле"}, status_code=400)
    with get_session() as session:
        vacancy = session.get(Vacancy, vacancy_id)
        if vacancy is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        letter = vacancy.letter or llm.generate_letter(profile, vacancy)
        ok, detail = hh.apply(vacancy.hh_id, profile.resume_id, letter)
        vacancy.letter = letter
        vacancy.status = "applied" if ok else "failed"
        vacancy.status_detail = detail
        vacancy.applied_at = datetime.utcnow() if ok else None
        session.add(vacancy)
        session.commit()
    return JSONResponse({"ok": ok, "detail": detail}, status_code=200 if ok else 502)


@app.post("/api/skip/{vacancy_id}")
def api_skip(vacancy_id: int) -> JSONResponse:
    with get_session() as session:
        vacancy = session.get(Vacancy, vacancy_id)
        if vacancy is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        vacancy.status = "skipped"
        session.add(vacancy)
        session.commit()
    return JSONResponse({"ok": True})
