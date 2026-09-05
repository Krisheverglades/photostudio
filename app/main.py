import os
import shutil
from datetime import datetime, timedelta

from dotenv import load_dotenv
from fastapi import Cookie, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app import calendar_utils, email_utils
from app.database import Appointment, Client, Shoot, get_session, init_db
from app.pipeline import process_shoot
from app.version import get_version

load_dotenv()

app = FastAPI(title="Studio")
BASE_DIR = os.path.dirname(__file__)
UPLOAD_ROOT = os.path.join(BASE_DIR, "uploads")
PROCESSED_ROOT = os.path.join(BASE_DIR, "processed")
os.makedirs(UPLOAD_ROOT, exist_ok=True)
os.makedirs(PROCESSED_ROOT, exist_ok=True)
os.makedirs(os.path.join(PROCESSED_ROOT, "_portfolio"), exist_ok=True)

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
app.mount("/media", StaticFiles(directory=PROCESSED_ROOT), name="media")

STUDIO_NAME = os.getenv("STUDIO_NAME", "Your Studio Name")
STUDIO_TAGLINE = os.getenv("STUDIO_TAGLINE", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")


@app.on_event("startup")
def on_startup():
    init_db()
    os.makedirs(UPLOAD_ROOT, exist_ok=True)
    os.makedirs(PROCESSED_ROOT, exist_ok=True)


# ───────────────────────── Public: portfolio + booking ─────────────────────────

@app.get("/", response_class=HTMLResponse)
def portfolio(request: Request):
    with get_session() as session:
        # "Portfolio" pulls from any shoot folder marked as public via a
        # `portfolio/` subfolder you curate manually — see README.
        portfolio_dir = os.path.join(PROCESSED_ROOT, "_portfolio")
        os.makedirs(portfolio_dir, exist_ok=True)
        images = sorted(os.listdir(portfolio_dir))
    return templates.TemplateResponse(
        "portfolio.html",
        {
            "request": request,
            "studio_name": STUDIO_NAME,
            "tagline": STUDIO_TAGLINE,
            "images": images,
        },
    )


@app.get("/book", response_class=HTMLResponse)
def book_page(request: Request):
    busy = calendar_utils.get_busy_slots()
    return templates.TemplateResponse(
        "book.html",
        {"request": request, "studio_name": STUDIO_NAME, "busy_slots": busy},
    )


@app.post("/book")
def book_appointment(
    name: str = Form(...),
    email: str = Form(...),
    notes: str = Form(""),
    start_time: str = Form(...),  # ISO string from the calendar widget
):
    start = datetime.fromisoformat(start_time)
    end = start + timedelta(hours=1)

    event_id = calendar_utils.create_event(
        summary=f"Session: {name}",
        description=notes or "Booked via studio website",
        start=start,
        end=end,
    )

    with get_session() as session:
        appt = Appointment(
            client_name=name,
            client_email=email,
            notes=notes,
            start_time=start,
            end_time=end,
            google_event_id=event_id,
        )
        session.add(appt)
        session.commit()

    email_utils.send_booking_confirmation(email, name, start.strftime("%A, %B %d at %I:%M %p"))
    return RedirectResponse(url="/book?confirmed=1", status_code=303)


# ───────────────────────── Client gallery access ─────────────────────────

@app.get("/gallery/{access_key}", response_class=HTMLResponse)
def client_gallery(request: Request, access_key: str):
    with get_session() as session:
        shoot = session.exec(select(Shoot).where(Shoot.access_key == access_key)).first()
        if not shoot:
            raise HTTPException(status_code=404, detail="Gallery not found. Check your link or access key.")
        client = session.get(Client, shoot.client_id)

    best_dir = os.path.join(shoot.folder_path, "best")
    images = sorted(os.listdir(best_dir)) if os.path.isdir(best_dir) else []
    rel_dir = os.path.relpath(best_dir, PROCESSED_ROOT)

    return templates.TemplateResponse(
        "client_gallery.html",
        {
            "request": request,
            "studio_name": STUDIO_NAME,
            "client_name": client.name,
            "shoot_title": shoot.title,
            "images": images,
            "media_dir": rel_dir,
            "status": shoot.status,
        },
    )


# ───────────────────────── Admin: upload + process a shoot ─────────────────────────

@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, pw: str = ""):
    if pw != ADMIN_PASSWORD:
        return templates.TemplateResponse("admin_login.html", {"request": request})
    with get_session() as session:
        shoots = session.exec(select(Shoot)).all()
    return templates.TemplateResponse(
        "admin_upload.html",
        {"request": request, "pw": pw, "shoots": shoots, "version": get_version()},
    )


@app.post("/admin/create_shoot")
async def create_shoot(
    pw: str = Form(...),
    client_name: str = Form(...),
    client_email: str = Form(...),
    shoot_title: str = Form(...),
    best_count: int = Form(100),
    style: str = Form("natural"),
    target_aspect: str = Form(""),  # e.g. "4:5", "1:1", "16:9", or blank = no crop
    files: list[UploadFile] = None,
):
    if pw != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Bad admin password")

    with get_session() as session:
        client = session.exec(select(Client).where(Client.email == client_email)).first()
        if not client:
            client = Client(name=client_name, email=client_email)
            session.add(client)
            session.commit()
            session.refresh(client)

        safe_title = "".join(c if c.isalnum() or c in "-_ " else "_" for c in shoot_title)
        input_dir = os.path.join(UPLOAD_ROOT, f"{client.id}_{safe_title}")
        output_dir = os.path.join(PROCESSED_ROOT, f"{client.id}_{safe_title}")
        os.makedirs(input_dir, exist_ok=True)

        for f in files or []:
            dest = os.path.join(input_dir, f.filename)
            with open(dest, "wb") as out:
                shutil.copyfileobj(f.file, out)

        shoot = Shoot(
            client_id=client.id,
            title=shoot_title,
            folder_path=output_dir,
            total_photos=len(files or []),
            best_count=best_count,
            status="processing",
        )
        session.add(shoot)
        session.commit()
        session.refresh(shoot)

    aspect_ratio = None
    if target_aspect and ":" in target_aspect:
        w, h = target_aspect.split(":")
        aspect_ratio = float(w) / float(h)

    # NOTE: for real shoot volumes (hundreds-thousands of photos), move this
    # to a background task/queue (e.g. Celery, RQ, or FastAPI BackgroundTasks)
    # instead of running inline in the request.
    scores = process_shoot(
        input_dir=input_dir,
        output_dir=output_dir,
        best_count=best_count,
        target_aspect=aspect_ratio,
        style=style,
    )

    with get_session() as session:
        shoot = session.get(Shoot, shoot.id)
        shoot.status = "ready"
        shoot.best_count = min(best_count, len(scores))
        session.add(shoot)
        session.commit()
        session.refresh(shoot)
        access_key = shoot.access_key

    sent = email_utils.send_gallery_email(client_email, client_name, shoot_title, access_key)

    with get_session() as session:
        shoot = session.get(Shoot, shoot.id)
        shoot.email_sent = sent
        session.add(shoot)
        session.commit()

    return RedirectResponse(url=f"/admin?pw={pw}", status_code=303)


@app.post("/admin/add_to_portfolio")
def add_to_portfolio(pw: str = Form(...), shoot_id: int = Form(...), filenames: str = Form(...)):
    if pw != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Bad admin password")
    with get_session() as session:
        shoot = session.get(Shoot, shoot_id)
    portfolio_dir = os.path.join(PROCESSED_ROOT, "_portfolio")
    os.makedirs(portfolio_dir, exist_ok=True)
    best_dir = os.path.join(shoot.folder_path, "best")
    for name in filenames.split(","):
        name = name.strip()
        src = os.path.join(best_dir, name)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(portfolio_dir, f"{shoot_id}_{name}"))
    return RedirectResponse(url=f"/admin?pw={pw}", status_code=303)
