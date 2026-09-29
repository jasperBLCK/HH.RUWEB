from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class Token(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    access_token: str
    refresh_token: str = ""
    expires_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Profile(SQLModel, table=True):
    """Данные соискателя, на которые опирается подбор и письма."""

    id: Optional[int] = Field(default=None, primary_key=True)
    full_name: str = ""
    resume_id: str = ""
    resume_text: str = ""
    skills: str = ""
    wishes: str = ""
    salary_min: int = 0
    remote_only: bool = True
    links: str = ""

    # Что искать: свободное описание для ИИ и готовые запросы для hh (по одному на строку)
    search_instruction: str = ""
    search_queries: str = ""
    exclude_words: str = ""

    # Автопилот
    auto_enabled: bool = False
    auto_min_score: int = 70
    auto_max_per_day: int = 10
    auto_interval_minutes: int = 60


class Vacancy(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    hh_id: str = Field(index=True, unique=True)
    name: str
    employer: str = ""
    url: str = ""
    salary: str = ""
    schedule: str = ""
    employment: str = ""
    experience: str = ""
    area: str = ""
    description: str = ""
    key_skills: str = ""
    score: int = 0
    score_reason: str = ""
    letter: str = ""
    status: str = "new"  # new | skipped | applied | failed
    status_detail: str = ""
    found_at: datetime = Field(default_factory=datetime.utcnow)
    applied_at: Optional[datetime] = None
    auto: bool = False


class LogEntry(SQLModel, table=True):
    """Журнал автопилота для отображения в панели."""

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    message: str = ""
