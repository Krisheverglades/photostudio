"""Private selection manifests and expiring, single-use gallery access."""
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from fastapi import HTTPException
from sqlalchemy import text
from sqlmodel import select
from app.database import GalleryAccess, GallerySession, Shoot, get_session


def selected(shoot):
    root = Path(shoot.folder_path)
    manifest = root / 'selection.json'
    if manifest.exists():
        rows = json.loads(manifest.read_text())
    else:
        # Existing galleries remain available for admin review, with a hard cap.
        rows = [{'filename': p.name, 'edited': True, 'flags': [], 'score': None}
                for p in sorted((root / 'best').glob('*')) if p.is_file()]
    result, seen = [], set()
    for row in rows:
        name = row['filename']
        path = root / 'best' / name
        if name != Path(name).name or name in seen or path.is_symlink() or not path.is_file():
            continue
        seen.add(name)
        result.append(row)
        if len(result) == 100:
            break
    return result


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def otp_digest(code, generation):
    return hashlib.pbkdf2_hmac('sha256', code.encode(), generation.encode(), 120000).hex()


def issue_code(shoot_id):
    code = f'{secrets.randbelow(1000000):06d}'
    with get_session() as session:
        access = session.get(GalleryAccess, shoot_id) or GalleryAccess(shoot_id=shoot_id)
        access.generation = secrets.token_hex(24)
        access.otp_hash = otp_digest(code, access.generation)
        access.expires = time.time() + 86400
        access.attempts = 0
        session.add(access)
        session.commit()
    return code


def verify_code(shoot_id, code):
    # Serialize attempts and consumption: a code cannot succeed twice concurrently.
    with get_session() as session:
        session.execute(text('BEGIN IMMEDIATE'))
        access = session.get(GalleryAccess, shoot_id)
        if not access or not access.otp_hash or access.expires < time.time() or access.attempts >= 5:
            return None
        access.attempts += 1
        valid = len(code) == 6 and code.isdigit() and hmac.compare_digest(otp_digest(code, access.generation), access.otp_hash)
        token = None
        if valid:
            access.otp_hash = ''
            token = secrets.token_urlsafe(32)
            session.add(GallerySession(token_hash=digest(token), shoot_id=shoot_id,
                                       generation=access.generation, expires=time.time() + 7 * 86400))
        session.add(access)
        session.commit()
        return token


def authorized(request, shoot):
    if not shoot.approved:
        return False
    token = request.cookies.get('gallery_session', '')
    if not token:
        return False
    with get_session() as session:
        entry = session.get(GallerySession, digest(token))
        access = session.get(GalleryAccess, shoot.id)
        return bool(entry and access and entry.shoot_id == shoot.id and entry.expires > time.time()
                    and entry.generation == access.generation)


def require_access(request, shoot):
    if not authorized(request, shoot):
        raise HTTPException(401, 'Enter the one-time code from your email to open this gallery')


def selected_path(shoot, filename):
    if filename not in {row['filename'] for row in selected(shoot)}:
        raise HTTPException(404, 'Photo not in the approved selection')
    return Path(shoot.folder_path) / 'best' / filename
