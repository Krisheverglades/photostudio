import os
import random
import secrets
import shutil
import io
import zipfile
import json
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from dotenv import load_dotenv
from fastapi import Cookie, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select

# Load configuration before importing modules that read environment variables
# during initialization (database, email, and calendar settings).
load_dotenv()

from app import calendar_utils, email_utils
from app.database import Appointment, Client, GalleryFeedback, Shoot, get_session, init_db
from app.pipeline import process_shoot
from app import delivery, jobs
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
app.mount("/media/_portfolio", StaticFiles(directory=os.path.join(PROCESSED_ROOT, "_portfolio")), name="media")

STUDIO_NAME = os.getenv("STUDIO_NAME", "Your Studio Name")
STUDIO_TAGLINE = os.getenv("STUDIO_TAGLINE", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")


def get_portfolio_images(limit: int = 10) -> list[str]:
    portfolio_dir = os.path.join(PROCESSED_ROOT, "_portfolio")
    os.makedirs(portfolio_dir, exist_ok=True)
    images = [
        name
        for name in os.listdir(portfolio_dir)
        if os.path.isfile(os.path.join(portfolio_dir, name))
    ]
    return random.sample(images, min(limit, len(images)))


def ensure_gallery_key(shoot: Shoot) -> str:
    if not shoot.access_key:
        shoot.access_key = secrets.token_urlsafe(16)
    return shoot.access_key


def admin_redirect(password: str, email_status: str = "") -> RedirectResponse:
    params = {"pw": password}
    if email_status:
        params["email"] = email_status
    return RedirectResponse(
        url=f"/admin?{urlencode(params)}",
        status_code=303,
    )


@app.on_event("startup")
def on_startup():
    init_db()
    jobs.resume()
    os.makedirs(UPLOAD_ROOT, exist_ok=True)
    os.makedirs(PROCESSED_ROOT, exist_ok=True)


# ───────────────────────── Public: portfolio + booking ─────────────────────────

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "studio_name": STUDIO_NAME,
            "tagline": STUDIO_TAGLINE,
            "images": get_portfolio_images(10),
        },
    )


@app.get("/portfolio", response_class=HTMLResponse)
def public_portfolio(request: Request):
    return templates.TemplateResponse(
        "portfolio.html",
        {
            "request": request,
            "studio_name": STUDIO_NAME,
            "images": get_portfolio_images(100),
        },
    )


@app.get("/camera", response_class=HTMLResponse)
def camera_page(request: Request):
    return templates.TemplateResponse(
        "camera.html",
        {"request": request, "studio_name": STUDIO_NAME},
    )


@app.get("/book", response_class=HTMLResponse)
def book_page(request: Request):
    busy = calendar_utils.get_busy_slots()
    return templates.TemplateResponse(
        "book.html",
        {
            "request": request,
            "studio_name": STUDIO_NAME,
            "busy_slots": busy,
            "background_images": get_portfolio_images(6),
        },
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

    if not delivery.authorized(request, shoot):
        return templates.TemplateResponse("gallery_unlock.html", {"request": request,
            "shoot_title": shoot.title, "access_key": access_key, "error": ""})
    rows = delivery.selected(shoot)
    images = [row["filename"] for row in rows]
    highlights = images[:10]
    random.shuffle(highlights)
    rel_dir = ""
    feedback_by_file = {item.filename: item for item in feedback}

    return templates.TemplateResponse(
        "client_gallery.html",
        {
            "request": request,
            "studio_name": STUDIO_NAME,
            "client_name": client.name,
            "shoot_title": shoot.title,
            "images": images,
            "highlights": highlights,
            "media_dir": rel_dir,
            "status": shoot.status,
            "access_key": access_key,
            "feedback": feedback_by_file,
            "favorite_count": sum(item.is_favorite for item in feedback),
        },
    )


@app.post("/gallery/{access_key}/feedback")
def save_gallery_feedback(
    request: Request,
    access_key: str,
    filename: str = Form(...),
    is_favorite: str = Form(""),
    note: str = Form(""),
):
    with get_session() as session:
        shoot = session.exec(select(Shoot).where(Shoot.access_key == access_key)).first()
        if not shoot:
            raise HTTPException(status_code=404, detail="Gallery not found")
        delivery.require_access(request, shoot)
        safe_filename = os.path.basename(filename.replace("\\", "/"))
        delivery.selected_path(shoot, safe_filename)
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
def download_gallery(request: Request, access_key: str):
    with get_session() as session:
        shoot = session.exec(select(Shoot).where(Shoot.access_key == access_key)).first()
        if not shoot:
            raise HTTPException(status_code=404, detail="Gallery not found")
        delivery.require_access(request, shoot)
        best_dir = os.path.abspath(os.path.join(shoot.folder_path, "best"))
        if not os.path.isdir(best_dir):
            raise HTTPException(status_code=404, detail="Gallery is not ready")
        archive = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for row in delivery.selected(shoot):
                filename = row["filename"]
                path = os.path.abspath(os.path.join(best_dir, filename))
                if os.path.isfile(path) and os.path.commonpath([path, best_dir]) == best_dir:
                    zip_file.write(path, arcname=filename)
        archive.seek(0)
    def archive_chunks():
        try:
            while chunk := archive.read(1024 * 1024):
                yield chunk
        finally:
            archive.close()
    return StreamingResponse(
        archive_chunks(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{shoot.access_key}-gallery.zip"'},
    )


# ───────────────────────── Admin: upload + process a shoot ─────────────────────────

@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, pw: str = "", page: str = "dashboard"):
    if pw != ADMIN_PASSWORD:
        return templates.TemplateResponse(
            "admin_login.html",
            {
                "request": request,
                "error": "Incorrect admin password." if pw else "",
                "background_images": get_portfolio_images(6),
                "page": page,
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
            "background_images": get_portfolio_images(6),
            "email_configured": email_utils.smtp_configured(),
            "page": page,
        },
    )


@app.get("/studio/portfolio", response_class=HTMLResponse)
def portfolio_workspace(request: Request, pw: str = ""):
    return admin_home(request, pw, "portfolio")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_workspace(request: Request, pw: str = ""):
    return admin_home(request, pw, "dashboard")


@app.get("/studio", response_class=HTMLResponse)
def studio_home(request: Request, pw: str = ""):
    return RedirectResponse(url=f"/dashboard?{urlencode({'pw': pw})}", status_code=303)


@app.post("/admin/create_shoot")
async def create_shoot(
    pw: str = Form(...),
    client_name: str = Form(...),
    client_email: str = Form(""),
    shoot_title: str = Form(...),
    best_count: int = Form(100),
    edit_count: int = Form(20),
    style: str = Form("natural"),
    target_aspect: str = Form(""),  # e.g. "4:5", "1:1", "16:9", or blank = no crop
    files: list[UploadFile] = None,
):
    if pw != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Bad admin password")
    if not 1 <= best_count <= 100 or not 0 <= edit_count <= best_count:
        raise HTTPException(400, "Choose 1–100 selected photos and 0–selection count edits")
    if style not in {"natural", "warm", "moody", "bright_airy"} or target_aspect not in {"", "4:5", "1:1", "16:9", "3:2"}:
        raise HTTPException(400, "Invalid editing options")
    if not files or not client_name.strip() or not shoot_title.strip():
        raise HTTPException(400, "Add a client, shoot title, and photos")
    allowed = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
    if any(Path(f.filename or "").suffix.lower() not in allowed for f in files):
        raise HTTPException(400, "Use JPEG, PNG, WebP, or TIFF. Export camera RAW files first.")
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
        folder = f"{client.id}_{safe_title[:70]}_{secrets.token_hex(8)}"
        input_dir = os.path.join(UPLOAD_ROOT, folder)
        output_dir = os.path.join(PROCESSED_ROOT, folder)
        os.makedirs(input_dir, exist_ok=True)

        for index, f in enumerate(files or []):
            filename = os.path.basename((f.filename or "").replace("\\", "/"))
            if not filename:
                continue
            dest = os.path.join(input_dir, f"{index + 1:05d}_{filename}")
            with open(dest, "wb") as out:
                shutil.copyfileobj(f.file, out)

        shoot = Shoot(
            client_id=client.id,
            title=shoot_title,
            folder_path=output_dir,
            total_photos=len(files or []),
            best_count=best_count,
            status="queued",
            edit_count=edit_count,
            input_path=input_dir,
            style=style,
            aspect=target_aspect,
        )
        session.add(shoot)
        session.commit()
        session.refresh(shoot)

    jobs.enqueue(shoot.id)
    return RedirectResponse(url=f"/studio/portfolio?{urlencode({'pw': pw})}", status_code=303)


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

        if shoot.status in {"queued", "processing"}:
            raise HTTPException(409, "Wait for processing to finish before deleting")
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
        if not shoot.approved or not delivery.selected(shoot):
            raise HTTPException(400, "Review and approve the selection before sending")
        client.email = client_email
        access_key = ensure_gallery_key(shoot)
        session.add(client)
        session.add(shoot)
        session.commit()
        code = delivery.issue_code(shoot.id)
        sent = email_utils.send_gallery_email(
            client_email, client.name, shoot.title, access_key, code
        )
        shoot.email_sent = sent
        shoot.status = "delivered" if sent else "ready"
        session.add(shoot)
        session.commit()

    return admin_redirect(pw, "sent" if sent else "failed")


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

@app.middleware("http")
async def private_response_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(("/gallery/", "/admin", "/studio", "/dashboard")):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@app.post('/gallery/{access_key}/unlock')
def unlock_gallery(request: Request, access_key: str, code: str = Form(...)):
    with get_session() as session:
        shoot = session.exec(select(Shoot).where(Shoot.access_key == access_key)).first()
    if not shoot or not shoot.approved:
        raise HTTPException(404, 'Gallery not ready')
    token = delivery.verify_code(shoot.id, code.strip())
    if not token:
        return templates.TemplateResponse('gallery_unlock.html', {'request': request,
            'shoot_title': shoot.title, 'access_key': access_key,
            'error': 'Code is invalid, expired, or already used. Ask the studio for a new code.'}, status_code=401)
    response = RedirectResponse(f'/gallery/{access_key}', status_code=303)
    response.set_cookie('gallery_session', token, max_age=7 * 86400, httponly=True,
                        secure=request.url.scheme == 'https', samesite='strict', path=f'/gallery/{access_key}')
    return response


@app.get('/gallery/{access_key}/files/{filename}')
def gallery_file(request: Request, access_key: str, filename: str, download: bool = False):
    with get_session() as session:
        shoot = session.exec(select(Shoot).where(Shoot.access_key == access_key)).first()
    if not shoot:
        raise HTTPException(404, 'Gallery not found')
    delivery.require_access(request, shoot)
    path = delivery.selected_path(shoot, filename)
    return FileResponse(path, filename=filename if download else None)


def admin_shoot(pw, shoot_id):
    if pw != ADMIN_PASSWORD:
        raise HTTPException(403, 'Bad admin password')
    with get_session() as session:
        shoot = session.get(Shoot, shoot_id)
    if not shoot:
        raise HTTPException(404, 'Shoot not found')
    return shoot


@app.get('/admin/shoot/{shoot_id}', response_class=HTMLResponse)
def review_shoot(request: Request, shoot_id: int, pw: str = ''):
    shoot = admin_shoot(pw, shoot_id)
    with get_session() as session:
        client = session.get(Client, shoot.client_id)
    return templates.TemplateResponse('shoot_review.html', {'request': request, 'shoot': shoot,
        'client': client, 'pw': pw, 'photos': delivery.selected(shoot)})


@app.get('/admin/shoot/{shoot_id}/files/{filename}')
def review_file(shoot_id: int, filename: str, pw: str = ''):
    shoot = admin_shoot(pw, shoot_id)
    return FileResponse(delivery.selected_path(shoot, filename))


@app.post('/admin/shoot/{shoot_id}/approve')
def approve_shoot(shoot_id: int, pw: str = Form(...), filenames: list[str] = Form(...)):
    shoot = admin_shoot(pw, shoot_id)
    if shoot.status not in {'review', 'ready'} or (shoot.email_sent and shoot.approved):
        raise HTTPException(409, 'Only undelivered completed shoots can be approved')
    rows = delivery.selected(shoot)
    names = set(filenames)
    if not names or not names.issubset({r['filename'] for r in rows}):
        raise HTTPException(400, 'Choose photographs from this selection')
    chosen = [r for r in rows if r['filename'] in names]
    manifest = Path(shoot.folder_path) / 'selection.json'
    manifest.with_suffix('.tmp').write_text(json.dumps(chosen))
    os.replace(manifest.with_suffix('.tmp'), manifest)
    with get_session() as session:
        current = session.get(Shoot, shoot_id)
        current.email_sent = False
        current.approved = True
        current.status = 'ready'
        current.best_count = len(chosen)
        current.edit_count = sum(r['edited'] for r in chosen)
        session.add(current)
        session.commit()
    return RedirectResponse(f'/admin/shoot/{shoot_id}?{urlencode({"pw": pw})}', status_code=303)


@app.post('/admin/shoot/{shoot_id}/retry')
def retry_shoot(shoot_id: int, pw: str = Form(...)):
    shoot = admin_shoot(pw, shoot_id)
    if shoot.status != 'failed' or not shoot.input_path:
        raise HTTPException(409, 'Only failed uploads can be retried')
    with get_session() as session:
        current = session.get(Shoot, shoot_id)
        current.status = 'queued'
        session.add(current)
        session.commit()
    jobs.enqueue(shoot_id)
    return RedirectResponse(f'/admin/shoot/{shoot_id}?{urlencode({"pw": pw})}', status_code=303)

@app.get('/admin/editor', response_class=HTMLResponse)
def editor_page(request: Request, pw: str = '', message: str = ''):
    if pw != ADMIN_PASSWORD:
        raise HTTPException(403, 'Bad admin password')
    from app.retouch import settings
    return templates.TemplateResponse('editor.html', {'request': request, 'pw': pw,
        'config': settings(), 'configured': bool(os.getenv('RETOUCH_MCP_URL')), 'message': message})


@app.post('/admin/editor')
def configure_editor(pw: str = Form(...), tool: str = Form('retouch_image'),
                     instructions: str = Form(...), enabled: str = Form('')):
    if pw != ADMIN_PASSWORD:
        raise HTTPException(403, 'Bad admin password')
    from app.retouch import MCPConnection, save_settings
    if enabled:
        try:
            with MCPConnection() as connection:
                connection.check_tool(tool)
        except Exception:
            return RedirectResponse('/admin/editor?' + urlencode({'pw': pw,
                'message': 'Connection failed. Check the server endpoint, credentials, and compatible image tool. Settings were not changed.'}), status_code=303)
    save_settings({'enabled': bool(enabled), 'tool': tool, 'instructions': instructions[:4000]})
    return RedirectResponse('/admin/editor?' + urlencode({'pw': pw,
        'message': 'MCP tool connected. Edits will be sent to this provider.' if enabled else 'Built-in tone and color editing enabled; external AI is off.'}), status_code=303)
