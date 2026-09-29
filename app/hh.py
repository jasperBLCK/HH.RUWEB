import re
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx
from sqlmodel import select

from app.config import settings
from app.db import get_session
from app.models import Token

API = "https://api.hh.ru"
OAUTH = "https://hh.ru/oauth"

TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    text = re.sub(r"<li[^>]*>", "\n— ", text or "")
    text = re.sub(r"</(p|div|ul|li|br)[^>]*>", "\n", text)
    text = TAG_RE.sub("", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def authorize_url(state: str = "hhruweb") -> str:
    return (
        f"{OAUTH}/authorize?response_type=code"
        f"&client_id={settings.hh_client_id}"
        f"&redirect_uri={settings.hh_redirect_uri}"
        f"&state={state}"
    )


def _store_token(payload: dict[str, Any]) -> Token:
    with get_session() as session:
        token = session.exec(select(Token).order_by(Token.id.desc())).first()
        if token is None:
            token = Token(access_token="")
        token.access_token = payload["access_token"]
        token.refresh_token = payload.get("refresh_token", token.refresh_token)
        token.expires_at = datetime.utcnow() + timedelta(seconds=payload.get("expires_in", 0))
        session.add(token)
        session.commit()
        session.refresh(token)
        return token


def exchange_code(code: str) -> Token:
    resp = httpx.post(
        f"{OAUTH}/token",
        data={
            "grant_type": "authorization_code",
            "client_id": settings.hh_client_id,
            "client_secret": settings.hh_client_secret,
            "redirect_uri": settings.hh_redirect_uri,
            "code": code,
        },
        headers={"User-Agent": settings.hh_user_agent},
        timeout=30,
    )
    resp.raise_for_status()
    return _store_token(resp.json())


def save_manual_token(access_token: str) -> Token:
    return _store_token({"access_token": access_token, "expires_in": 0})


def current_token() -> Optional[Token]:
    with get_session() as session:
        return session.exec(select(Token).order_by(Token.id.desc())).first()


def refresh_token() -> Optional[Token]:
    token = current_token()
    if token is None or not token.refresh_token:
        return None
    resp = httpx.post(
        f"{OAUTH}/token",
        data={"grant_type": "refresh_token", "refresh_token": token.refresh_token},
        headers={"User-Agent": settings.hh_user_agent},
        timeout=30,
    )
    resp.raise_for_status()
    return _store_token(resp.json())


def _client() -> httpx.Client:
    token = current_token()
    headers = {"User-Agent": settings.hh_user_agent}
    if token:
        headers["Authorization"] = f"Bearer {token.access_token}"
    return httpx.Client(base_url=API, headers=headers, timeout=30)


def search_vacancies(
    text: str,
    per_page: int = 20,
    page: int = 0,
    remote_only: bool = True,
    salary_min: int = 0,
    extra: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"text": text, "per_page": per_page, "page": page, "order_by": "publication_time"}
    if remote_only:
        params["schedule"] = "remote"
    if salary_min:
        params["salary"] = salary_min
        params["only_with_salary"] = "true"
    if extra:
        params.update(extra)
    with _client() as client:
        resp = client.get("/vacancies", params=params)
        resp.raise_for_status()
        return resp.json().get("items", [])


def vacancy_details(hh_id: str) -> dict[str, Any]:
    with _client() as client:
        resp = client.get(f"/vacancies/{hh_id}")
        resp.raise_for_status()
        return resp.json()


def my_resumes() -> list[dict[str, Any]]:
    with _client() as client:
        resp = client.get("/resumes/mine")
        resp.raise_for_status()
        return resp.json().get("items", [])


def apply(vacancy_id: str, resume_id: str, message: str) -> tuple[bool, str]:
    """Отправляет отклик. Возвращает (успех, детали)."""
    with _client() as client:
        resp = client.post(
            "/negotiations",
            data={"vacancy_id": vacancy_id, "resume_id": resume_id, "message": message},
        )
    if resp.status_code in (201, 204):
        return True, "ok"
    return False, f"{resp.status_code}: {resp.text[:300]}"
