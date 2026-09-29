from pathlib import Path

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app import auto, hh, llm, service
from app.config import settings
from app.db import get_session, init_db
from app.models import Profile, Vacancy
from app.service import load_profile

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
    auto.start()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, status: str = "new") -> HTMLResponse:
    with get_session() as session:
        query = select(Vacancy).order_by(Vacancy.score.desc(), Vacancy.found_at.desc())
        if status != "all":
            query = query.where(Vacancy.status == status)
        vacancies = session.exec(query).all()
        all_vacancies = session.exec(select(Vacancy)).all()
    counts = {"all": len(all_vacancies)}
    for key in ("new", "applied", "skipped", "failed"):
        counts[key] = sum(1 for v in all_vacancies if v.status == key)
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "vacancies": vacancies,
            "status": status,
            "counts": counts,
            "profile": load_profile(),
            "authorized": hh.current_token() is not None,
            "llm_enabled": llm.enabled(),
            "logs": service.recent_logs(15),
            "applied_today": service.applied_today(),
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
    search_instruction: str = Form(""),
    search_queries: str = Form(""),
    exclude_words: str = Form(""),
    auto_enabled: bool = Form(False),
    auto_min_score: int = Form(70),
    auto_max_per_day: int = Form(10),
    auto_interval_minutes: int = Form(60),
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
        profile.search_instruction = search_instruction
        profile.search_queries = search_queries
        profile.exclude_words = exclude_words
        profile.auto_enabled = auto_enabled
        profile.auto_min_score = auto_min_score
        profile.auto_max_per_day = auto_max_per_day
        profile.auto_interval_minutes = auto_interval_minutes
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
    try:
        added, updated = service.run_search(profile, query, pages)
    except httpx.HTTPError as exc:
        return JSONResponse({"error": f"hh API: {exc}"}, status_code=502)
    return JSONResponse({"added": added, "updated": updated})


@app.post("/api/score/{vacancy_id}")
def api_score(vacancy_id: int) -> JSONResponse:
    try:
        score, reason = service.score(load_profile(), vacancy_id)
    except LookupError:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"score": score, "reason": reason})


@app.post("/api/letter/{vacancy_id}")
def api_letter(vacancy_id: int) -> JSONResponse:
    try:
        letter = service.write_letter(load_profile(), vacancy_id)
    except LookupError:
        return JSONResponse({"error": "not found"}, status_code=404)
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
    try:
        ok, detail = service.apply(profile, vacancy_id)
    except LookupError:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": ok, "detail": detail}, status_code=200 if ok else 502)


@app.post("/api/auto/run")
async def api_auto_run() -> JSONResponse:
    """Запускает один проход автопилота прямо сейчас."""
    return JSONResponse(await auto.run_cycle())


@app.post("/api/auto/toggle")
def api_auto_toggle() -> JSONResponse:
    with get_session() as session:
        profile = session.exec(select(Profile)).first() or Profile()
        profile.auto_enabled = not profile.auto_enabled
        session.add(profile)
        session.commit()
        enabled = profile.auto_enabled
    service.log("Автопилот включён" if enabled else "Автопилот выключен")
    return JSONResponse({"auto_enabled": enabled})


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
