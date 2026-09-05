import os
import secrets
from datetime import datetime
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine

DB_PATH = os.getenv("DB_PATH", "./app/studio.db")
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


class Client(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    email: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Shoot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    client_id: int = Field(foreign_key="client.id")
    title: str
    access_key: str = Field(default_factory=lambda: secrets.token_urlsafe(16), unique=True, index=True)
    folder_path: str
    total_photos: int = 0
    best_count: int = 0
    status: str = "pending"  # pending -> processing -> ready
    created_at: datetime = Field(default_factory=datetime.utcnow)
    email_sent: bool = False


class Appointment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    client_name: str
    client_email: str
    notes: Optional[str] = None
    start_time: datetime
    end_time: datetime
    google_event_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    return Session(engine)
