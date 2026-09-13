"""End-to-end checks use only temporary databases/images and mocked email."""
import io
import json
import os
import tempfile
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

TEST_ROOT = Path(tempfile.mkdtemp(prefix='studio-tests-'))
os.environ['DB_PATH'] = str(TEST_ROOT / 'studio.db')
os.environ['ADMIN_PASSWORD'] = 'test-only-password'
os.environ['RETOUCH_MCP_URL'] = ''
from PIL import Image
import numpy as np
from fastapi.testclient import TestClient
from sqlmodel import select
from app import main, database, delivery, jobs, pipeline, retouch
main.UPLOAD_ROOT = str(TEST_ROOT / 'uploads')
main.PROCESSED_ROOT = str(TEST_ROOT / 'processed')
os.makedirs(main.UPLOAD_ROOT)
os.makedirs(main.PROCESSED_ROOT)
database.init_db()
client = TestClient(main.app)
PW = 'test-only-password'


def photo(seed=1):
    rng = np.random.default_rng(seed)
    image = Image.fromarray(rng.integers(0, 255, (80, 64, 3), dtype=np.uint8))
    stream = io.BytesIO(); image.save(stream, 'JPEG'); return stream.getvalue()


def create(count=4, edits=2):
    with patch.object(jobs, 'enqueue'):
        response = client.post('/admin/create_shoot', data={'pw': PW, 'client_name': 'Test person',
            'client_email': 'test@example.invalid', 'shoot_title': 'Repeated shoot name',
            'best_count': count, 'edit_count': edits}, files=[('files', ('portrait.jpg', photo(n), 'image/jpeg')) for n in range(6)], follow_redirects=False)
    assert response.status_code == 303, response.text
    with database.get_session() as session:
        shoot = session.exec(select(database.Shoot).order_by(database.Shoot.id.desc())).first()
    jobs.run_job(shoot.id)
    with database.get_session() as session:
        return session.get(database.Shoot, shoot.id)


def test_selection_edit_counts_and_unique_shoot_storage():
    with patch.object(pipeline, 'auto_edit', wraps=pipeline.auto_edit) as edit:
        shoot = create()
        assert edit.call_count == 2
    assert shoot.status == 'review'
    rows = delivery.selected(shoot)
    assert len(rows) == 4 and sum(r['edited'] for r in rows) == 2
    assert len(list(Path(shoot.input_path).glob('*'))) == 6
    other = create()
    assert other.folder_path != shoot.folder_path and other.input_path != shoot.input_path
    assert len(delivery.selected(other)) == 4


def test_approval_otp_downloads_and_private_file_bypass():
    shoot = create()
    key = shoot.access_key
    rows = delivery.selected(shoot)
    base = f'/gallery/{key}'
    assert client.get(base).status_code == 200
    assert 'one-time code' in client.get(base).text
    assert client.get(base + '/files/' + rows[0]['filename']).status_code == 401
    assert client.get(base + '/download-all').status_code == 401
    assert client.get('/media/' + Path(shoot.folder_path).name + '/best/' + rows[0]['filename']).status_code == 404
    assert client.post('/admin/send_gallery_email', data={'pw': PW, 'shoot_id': shoot.id, 'client_email':'test@example.invalid'}).status_code == 400
    keep = [r['filename'] for r in rows[:3]]
    assert client.post(f'/admin/shoot/{shoot.id}/approve', data={'pw':PW, 'filenames':keep}, follow_redirects=False).status_code == 303
    with patch.object(main.email_utils, 'send_gallery_email', return_value=True) as mail:
        result = client.post('/admin/send_gallery_email', data={'pw': PW, 'shoot_id':shoot.id, 'client_email':'test@example.invalid'}, follow_redirects=False)
        assert result.status_code == 303
        code = mail.call_args.args[-1]
    assert len(code) == 6
    assert client.post(base + '/unlock', data={'code':code}, follow_redirects=False).status_code == 303
    assert client.post(base + '/unlock', data={'code':code}, follow_redirects=False).status_code == 401
    response = client.get(base)
    assert response.status_code == 200 and 'Download all' in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert client.get(base + '/files/' + keep[0]).status_code == 200
    assert client.get(base + '/files/' + rows[3]['filename']).status_code == 404
    archive = client.get(base + '/download-all')
    assert set(zipfile.ZipFile(io.BytesIO(archive.content)).namelist()) == set(keep)
    other = create()
    assert client.get(f'/gallery/{other.access_key}/files/' + delivery.selected(other)[0]['filename']).status_code == 401
    delivery.issue_code(shoot.id)
    assert client.get(base + '/files/' + keep[0]).status_code == 401


def test_otp_expiration_attempt_limits():
    shoot = create()
    code = delivery.issue_code(shoot.id)
    with database.get_session() as session:
        access = session.get(database.GalleryAccess, shoot.id); access.expires = time.time() - 1
        session.add(access); session.commit()
    assert delivery.verify_code(shoot.id, code) is None
    code = delivery.issue_code(shoot.id)
    wrong = '000000' if code != '000000' else '111111'
    for _ in range(5):
        assert delivery.verify_code(shoot.id, wrong) is None
    assert delivery.verify_code(shoot.id, code) is None


def test_count_validation_and_bad_uploads():
    for best, edits in [(101,20),(0,0),(10,20),(20,-1)]:
        response = client.post('/admin/create_shoot', data={'pw':PW, 'client_name':'A', 'shoot_title':'B', 'best_count':best, 'edit_count':edits}, files={'files':('x.jpg',photo(),'image/jpeg')})
        assert response.status_code == 400
    assert client.post('/admin/create_shoot', data={'pw':PW, 'client_name':'A', 'shoot_title':'B'}, files={'files':('bad.html',b'<html>', 'text/html')}).status_code == 400


def test_cap_100_corrupt_and_only_selected_edited():
    source = TEST_ROOT / 'bulk'; source.mkdir(exist_ok=True)
    for n in range(105): (source/f'{n}.jpg').write_bytes(photo(n))
    (source/'corrupt.jpg').write_bytes(b'not an image')
    output = TEST_ROOT / 'bulk-output'
    with patch.object(pipeline, 'auto_edit', wraps=pipeline.auto_edit) as edit:
        scores = pipeline.process_shoot(str(source),str(output),100,edit_count=20)
        assert len(scores) == 105 and edit.call_count == 20
    rows = json.loads((output/'selection.json').read_text())
    assert len(rows) == len(list((output/'best').glob('*'))) == 100
    assert not (output/'all').exists()


def test_admin_pages_and_legacy_gallery_can_be_reviewed():
    assert client.get('/studio/portfolio',params={'pw':PW}).status_code == 200
    assert 'edit_count' in client.get('/studio/portfolio',params={'pw':PW}).text
    assert client.get('/dashboard',params={'pw':PW}).status_code == 200
    assert client.get('/admin/editor',params={'pw':PW}).status_code == 200
    shoot = create()
    with database.get_session() as session:
        entry = session.get(database.Shoot, shoot.id); entry.status='ready';entry.email_sent=True
        session.add(entry);session.commit()
    response = client.post(f'/admin/shoot/{shoot.id}/approve',data={'pw':PW,'filenames':[r['filename'] for r in delivery.selected(shoot)]},follow_redirects=False)
    assert response.status_code == 303


def test_mcp_adapter_contract_and_failure():
    config={'enabled':True,'tool':'retouch_image','instructions':'Keep identity'}
    import base64
    with patch.object(retouch.MCPConnection, '__enter__', return_value=None):
        pass
    class FakeConnection:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def rpc(self,method,params):
            assert method == 'tools/call'
            assert set(params['arguments']) == {'image_base64','mime_type','instructions'}
            return {'content':[{'type':'image','data':base64.b64encode(photo()).decode()}]}
    with patch.object(retouch,'MCPConnection',FakeConnection):
        edited = retouch.MCPRetouchProvider(config).process(Image.open(io.BytesIO(photo())), 'unused')
        assert edited.size == (64,80)
    assert isinstance(retouch.get_retouch_provider(), retouch.NoopRetouchProvider)


def test_one_code_cannot_be_consumed_concurrently():
    from concurrent.futures import ThreadPoolExecutor
    shoot = create()
    code = delivery.issue_code(shoot.id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens = list(pool.map(lambda _: delivery.verify_code(shoot.id, code), range(2)))
    assert sum(token is not None for token in tokens) == 1


def test_additive_legacy_database_migration():
    import sqlite3
    from sqlmodel import create_engine
    path = TEST_ROOT / 'legacy.db'
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE shoot (id INTEGER PRIMARY KEY, client_id INTEGER, title VARCHAR,
        access_key VARCHAR, folder_path VARCHAR, total_photos INTEGER, best_count INTEGER,
        status VARCHAR, created_at DATETIME, email_sent BOOLEAN);
      INSERT INTO shoot VALUES (1,1,'Existing shoot','existing-key','/private',120,100,'ready',NULL,1);
      CREATE TABLE appointment (id INTEGER PRIMARY KEY);
    """)
    con.commit(); con.close()
    with patch.object(database, 'engine', create_engine(f'sqlite:///{path}')):
        database.init_db()
    con = sqlite3.connect(path)
    row = con.execute('SELECT title,best_count,edit_count,approved FROM shoot').fetchone()
    assert row == ('Existing shoot',100,20,0)
    assert 'status' in [r[1] for r in con.execute('PRAGMA table_info(appointment)')]
    con.close()
