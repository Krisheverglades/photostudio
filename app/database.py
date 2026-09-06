import os
import secrets
from datetime import datetime
from typing import Optional

from sqlalchemy import text
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


class GalleryFeedback(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    shoot_id: int = Field(foreign_key="shoot.id", index=True)
    filename: str
    is_favorite: bool = False
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Appointment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    client_name: str
    client_email: str
    notes: Optional[str] = None
    start_time: datetime
    end_time: datetime
    google_event_id: Optional[str] = None
    status: str = "requested"  # requested -> confirmed -> completed/cancelled
    created_at: datetime = Field(default_factory=datetime.utcnow)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(appointment)"))
        }
        if "status" not in columns:
            connection.execute(
                text("ALTER TABLE appointment ADD COLUMN status VARCHAR DEFAULT 'requested'")
            )
            connection.commit()


def get_session() -> Session:
    return Session(engine)
