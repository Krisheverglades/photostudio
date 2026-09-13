"""Single-worker persistent shoot queue. Unfinished jobs resume after restart."""
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from sqlmodel import select
from app.database import Shoot, get_session
from app.pipeline import process_shoot
from app.delivery import selected

executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='shoot')
lock = Lock()
pending = set()


def enqueue(shoot_id):
    with lock:
        if shoot_id in pending:
            return
        pending.add(shoot_id)
    executor.submit(run_job, shoot_id)


def resume():
    with get_session() as session:
        ids = [s.id for s in session.exec(select(Shoot)).all()
               if s.status in {'queued', 'processing'} and s.input_path]
    for shoot_id in ids:
        enqueue(shoot_id)


def run_job(shoot_id):
    try:
        with get_session() as session:
            shoot = session.get(Shoot, shoot_id)
            if not shoot or shoot.approved:
                return
            shoot.status = 'processing'
            shoot.error = ''
            session.add(shoot)
            session.commit()
            session.refresh(shoot)
        ratio = None
        if shoot.aspect:
            w, h = map(float, shoot.aspect.split(':'))
            ratio = w / h
        scores = process_shoot(shoot.input_path, shoot.folder_path, shoot.best_count,
                               ratio, shoot.style, shoot.edit_count)
        with get_session() as session:
            current = session.get(Shoot, shoot_id)
            if current:
                rows = selected(current)
                current.best_count = len(rows)
                current.edit_count = sum(r['edited'] for r in rows)
                current.status = 'review'
                session.add(current)
                session.commit()
    except Exception:
        logging.exception('Shoot %s processing failed', shoot_id)
        with get_session() as session:
            current = session.get(Shoot, shoot_id)
            if current:
                current.status = 'failed'
                current.error = 'Processing failed. Check supported image files and the editing connection, then retry.'
                session.add(current)
                session.commit()
    finally:
        with lock:
            pending.discard(shoot_id)
