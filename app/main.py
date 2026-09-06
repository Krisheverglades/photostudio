import os
import random
import shutil
import io
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from dotenv import load_dotenv
from fastapi import Cookie, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select

# Load configuration before importing modules that read environment variables
# during initialization (database, email, and calendar settings).
load_dotenv()

from app import calendar_utils, email_utils
from app.database import Appointment, Client, GalleryFeedback, Shoot, get_session, init_db
from app.pipeline import process_shoot
from app.version import get_version

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


def admin_redirect(password: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/admin?{urlencode({'pw': password})}",
        status_code=303,
    )


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
        images = [
            name
            for name in os.listdir(portfolio_dir)
            if os.path.isfile(os.path.join(portfolio_dir, name))
        ]
        images = random.sample(images, min(10, len(images)))
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
    duration_hours: float = Form(...),
):
    try:
        start = datetime.fromisoformat(start_time)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Choose a valid appointment time.") from exc

    if start.tzinfo is None:
        raise HTTPException(status_code=400, detail="Choose a valid appointment time.")
    if not 0.5 <= duration_hours <= 9:
        raise HTTPException(status_code=400, detail="Shoot duration must be between 0.5 and 9 hours.")

    now = datetime.now(start.tzinfo) if start.tzinfo else datetime.now()
    if not now < start <= now + timedelta(days=14):
        raise HTTPException(status_code=400, detail="Choose a time within the next 14 days.")
    if start.minute or start.second or start.microsecond or start.hour not in set(range(9, 18)):
        raise HTTPException(status_code=400, detail="Choose one of the available appointment slots.")

    end = start + timedelta(hours=duration_hours)
    if end.hour > 18 or (end.hour == 18 and (end.minute or end.second or end.microsecond)):
        raise HTTPException(status_code=400, detail="This duration does not fit within the 09:00–18:00 studio hours.")
    buffer_start = start - timedelta(hours=3)
    buffer_end = end + timedelta(hours=3)
    db_start = start.astimezone(timezone.utc).replace(tzinfo=None)
    db_end = end.astimezone(timezone.utc).replace(tzinfo=None)
    db_buffer_start = db_start - timedelta(hours=3)
    db_buffer_end = db_end + timedelta(hours=3)
    busy = calendar_utils.get_busy_slots(days_ahead=14)
    if any(
        buffer_start < datetime.fromisoformat(slot["end"].replace("Z", "+00:00"))
        and buffer_end > datetime.fromisoformat(slot["start"].replace("Z", "+00:00"))
        for slot in busy
    ):
        raise HTTPException(status_code=409, detail="That appointment slot is no longer available.")

    with get_session() as session:
        existing = session.exec(
            select(Appointment).where(
                Appointment.start_time < db_buffer_end,
                Appointment.end_time > db_buffer_start,
            )
        ).first()
    if existing:
        raise HTTPException(status_code=409, detail="That appointment slot is no longer available.")

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
            start_time=db_start,
            end_time=db_end,
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
        feedback = session.exec(
            select(GalleryFeedback).where(GalleryFeedback.shoot_id == shoot.id)
        ).all()

    best_dir = os.path.join(shoot.folder_path, "best")
    images = sorted(os.listdir(best_dir)) if os.path.isdir(best_dir) else []
    rel_dir = os.path.relpath(best_dir, PROCESSED_ROOT).replace(os.sep, "/")
    feedback_by_file = {item.filename: item for item in feedback}

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
            "access_key": access_key,
            "feedback": feedback_by_file,
            "favorite_count": sum(item.is_favorite for item in feedback),
        },
    )


@app.post("/gallery/{access_key}/feedback")
def save_gallery_feedback(
    access_key: str,
    filename: str = Form(...),
    is_favorite: str = Form(""),
    note: str = Form(""),
):
    with get_session() as session:
        shoot = session.exec(select(Shoot).where(Shoot.access_key == access_key)).first()
        if not shoot:
            raise HTTPException(status_code=404, detail="Gallery not found")
        safe_filename = os.path.basename(filename.replace("\\", "/"))
        best_dir = os.path.abspath(os.path.join(shoot.folder_path, "best"))
        image_path = os.path.abspath(os.path.join(best_dir, safe_filename))
        if os.path.commonpath([image_path, best_dir]) != best_dir or not os.path.isfile(image_path):
            raise HTTPException(status_code=400, detail="Photo not found in this gallery")
        item = session.exec(
            select(GalleryFeedback).where(
                GalleryFeedback.shoot_id == shoot.id,
                GalleryFeedback.filename == safe_filename,
            )
        ).first()
        if not item:
            item = GalleryFeedback(shoot_id=shoot.id, filename=safe_filename)
        item.is_favorite = is_favorite == "true"
        item.note = note.strip() or None
        item.updated_at = datetime.utcnow()
        session.add(item)
        session.commit()
    return RedirectResponse(url=f"/gallery/{access_key}", status_code=303)


@app.get("/gallery/{access_key}/download-all")
def download_gallery(access_key: str):
    with get_session() as session:
        shoot = session.exec(select(Shoot).where(Shoot.access_key == access_key)).first()
        if not shoot:
            raise HTTPException(status_code=404, detail="Gallery not found")
        best_dir = os.path.abspath(os.path.join(shoot.folder_path, "best"))
        if not os.path.isdir(best_dir):
            raise HTTPException(status_code=404, detail="Gallery is not ready")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for filename in sorted(os.listdir(best_dir)):
                path = os.path.abspath(os.path.join(best_dir, filename))
                if os.path.isfile(path) and os.path.commonpath([path, best_dir]) == best_dir:
                    zip_file.write(path, arcname=filename)
        archive.seek(0)
    return StreamingResponse(
        archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{shoot.access_key}-gallery.zip"'},
    )


# ───────────────────────── Admin: upload + process a shoot ─────────────────────────

@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, pw: str = ""):
    if pw != ADMIN_PASSWORD:
        return templates.TemplateResponse(
            "admin_login.html",
            {
                "request": request,
                "error": "Incorrect admin password." if pw else "",
            },
        )
    with get_session() as session:
        shoots = session.exec(select(Shoot)).all()
        appointments = session.exec(
            select(Appointment).order_by(Appointment.start_time)
        ).all()
        clients = {client.id: client for client in session.exec(select(Client)).all()}
        upcoming_appointments = [
            appointment
            for appointment in appointments
            if appointment.start_time >= datetime.utcnow()
            and appointment.status != "cancelled"
        ][:5]
        appointment_stats = {
            "total": len(appointments),
            "upcoming": len(upcoming_appointments),
            "requested": sum(item.status == "requested" for item in appointments),
            "confirmed": sum(item.status == "confirmed" for item in appointments),
        }
    return templates.TemplateResponse(
        "admin_upload.html",
        {
            "request": request,
            "pw": pw,
            "shoots": shoots,
            "appointments": appointments,
            "upcoming_appointments": upcoming_appointments,
            "appointment_stats": appointment_stats,
            "clients": clients,
            "version": get_version(),
        },
    )


@app.post("/admin/create_shoot")
async def create_shoot(
    pw: str = Form(...),
    client_name: str = Form(...),
    client_email: str = Form(""),
    shoot_title: str = Form(...),
    best_count: int = Form(100),
    style: str = Form("natural"),
    target_aspect: str = Form(""),  # e.g. "4:5", "1:1", "16:9", or blank = no crop
    files: list[UploadFile] = None,
):
    if pw != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Bad admin password")
    if best_count < 1:
        raise HTTPException(status_code=400, detail="Select at least one photo.")
    best_count = min(best_count, 100)
    client_email = client_email.strip()

    with get_session() as session:
        client = None
        if client_email:
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
            filename = os.path.basename((f.filename or "").replace("\\", "/"))
            if not filename:
                continue
            dest = os.path.join(input_dir, filename)
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

    sent = (
        email_utils.send_gallery_email(client_email, client_name, shoot_title, access_key)
        if client_email
        else False
    )

    with get_session() as session:
        shoot = session.get(Shoot, shoot.id)
        shoot.email_sent = sent
        session.add(shoot)
        session.commit()

    return admin_redirect(pw)


@app.post("/admin/upload_portfolio")
async def upload_portfolio(
    pw: str = Form(...),
    files: list[UploadFile] = None,
):
    if pw != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Bad admin password")

    portfolio_dir = os.path.join(PROCESSED_ROOT, "_portfolio")
    os.makedirs(portfolio_dir, exist_ok=True)
    allowed_extensions = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
    uploaded = 0
    for file in files or []:
        filename = os.path.basename((file.filename or "").replace("\\", "/"))
        extension = os.path.splitext(filename)[1].lower()
        if not filename or extension not in allowed_extensions:
            continue
        destination = os.path.join(portfolio_dir, filename)
        if os.path.exists(destination):
            stem, suffix = os.path.splitext(filename)
            destination = os.path.join(portfolio_dir, f"{stem}_{uploaded + 1}{suffix}")
        with open(destination, "wb") as output:
            shutil.copyfileobj(file.file, output)
        uploaded += 1

    return admin_redirect(pw)


@app.post("/admin/add_to_portfolio")
def add_to_portfolio(pw: str = Form(...), shoot_id: int = Form(...), filenames: str = Form(...)):
    if pw != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Bad admin password")
    with get_session() as session:
        shoot = session.get(Shoot, shoot_id)
    if not shoot:
        raise HTTPException(status_code=404, detail="Shoot not found")
    portfolio_dir = os.path.join(PROCESSED_ROOT, "_portfolio")
    os.makedirs(portfolio_dir, exist_ok=True)
    best_dir = os.path.join(shoot.folder_path, "best")
    for name in filenames.split(","):
        name = os.path.basename(name.strip().replace("\\", "/"))
        if not name:
            continue
        src = os.path.join(best_dir, name)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(portfolio_dir, f"{shoot_id}_{name}"))
    return admin_redirect(pw)


@app.post("/admin/delete_shoot")
def delete_shoot(pw: str = Form(...), shoot_id: int = Form(...)):
    if pw != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Bad admin password")

    with get_session() as session:
        shoot = session.get(Shoot, shoot_id)
        if not shoot:
            raise HTTPException(status_code=404, detail="Shoot not found")

        shoot_folder = os.path.abspath(shoot.folder_path)
        processed_root = os.path.abspath(PROCESSED_ROOT)
        if os.path.commonpath([shoot_folder, processed_root]) != processed_root:
            raise HTTPException(status_code=400, detail="Invalid shoot storage path")

        input_folder = os.path.join(UPLOAD_ROOT, os.path.basename(shoot_folder))
        if os.path.isdir(input_folder):
            shutil.rmtree(input_folder)
        if os.path.isdir(shoot_folder):
            shutil.rmtree(shoot_folder)

        session.delete(shoot)
        session.commit()

    return admin_redirect(pw)


@app.post("/admin/send_gallery_email")
def send_gallery_email(
    pw: str = Form(...),
    shoot_id: int = Form(...),
    client_email: str = Form(""),
):
    if pw != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Bad admin password")

    client_email = client_email.strip()
    if not client_email:
        raise HTTPException(status_code=400, detail="Enter a client email address")

    with get_session() as session:
        shoot = session.get(Shoot, shoot_id)
        if not shoot:
            raise HTTPException(status_code=404, detail="Shoot not found")
        client = session.get(Client, shoot.client_id)
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")
        client.email = client_email
        session.add(client)
        session.commit()
        sent = email_utils.send_gallery_email(
            client_email, client.name, shoot.title, shoot.access_key
        )
        shoot.email_sent = sent
        session.add(shoot)
        session.commit()

    return admin_redirect(pw)


@app.post("/admin/update_appointment")
def update_appointment(
    pw: str = Form(...),
    appointment_id: int = Form(...),
    status: str = Form(...),
):
    if pw != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Bad admin password")
    if status not in {"requested", "confirmed", "completed", "cancelled"}:
        raise HTTPException(status_code=400, detail="Invalid appointment status")
    with get_session() as session:
        appointment = session.get(Appointment, appointment_id)
        if not appointment:
            raise HTTPException(status_code=404, detail="Appointment not found")
        appointment.status = status
        session.add(appointment)
        session.commit()
    return admin_redirect(pw)
