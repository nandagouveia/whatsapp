import io
import json
import tempfile
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch
import server

class Integration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbpath = str(Path(self.tmp.name) / 'orders.sqlite3')
        self.storage_ok = True
        self.signed_calls = []
        def db():
            class TestConnection:
                def __init__(inner):
                    inner.conn = sqlite3.connect(self.dbpath)
                    inner.conn.row_factory = sqlite3.Row
                    inner.conn.execute('''CREATE TABLE IF NOT EXISTS pix_orders (
                       id TEXT PRIMARY KEY, session TEXT, amount INTEGER, gateway_id TEXT,
                       code TEXT, qr TEXT, status TEXT DEFAULT 'creating', expires TEXT,
                       created REAL, checked REAL DEFAULT 0)''')
                    inner.conn.commit()
                def execute(inner, sql, params=()):
                    return inner.conn.execute(sql.replace('%s','?'), params)
                def commit(inner): inner.conn.commit()
                def __enter__(inner): return inner
                def __exit__(inner, kind, value, tb):
                    if kind: inner.conn.rollback()
                    else: inner.conn.commit()
                    inner.conn.close()
            return TestConnection()
        def sign(seconds=None):
            self.signed_calls.append(seconds)
            if not self.storage_ok: raise server.Failure(503, 'Vídeo indisponível.')
            return 'https://project.supabase.co/storage/v1/object/sign/videos-pagos/completo.mp4?token=test'
        self.dbpatch = patch.object(server, 'db', db); self.dbpatch.start()
        self.lockpatch = patch.object(server, 'lock_session', lambda conn, session: None); self.lockpatch.start()
        self.signpatch = patch.object(server, 'signed_video_url', sign); self.signpatch.start()
        server.PUBLIC_URL = 'http://localhost:8080'
        server.COOKIE_SECURE = False
        self.cookie = ''
        self.calls = []
        self.provider_status = 'pending'
        self.wrong_amount = False
        self.failed_create = False
        self.oid = ''
        self.mock = patch.object(server, 'gateway', self.provider)
        self.mock.start()
    def tearDown(self):
        self.mock.stop(); self.dbpatch.stop(); self.lockpatch.stop(); self.signpatch.stop(); self.tmp.cleanup()
    def provider(self, path, payload=None):
        self.calls.append((path, payload))
        if payload:
            self.oid = payload['external_id']
            if self.failed_create: raise server.Failure(502, 'Gateway temporariamente indisponível.')
            return {'success': True, 'transaction': {'id': 'gateway-123', 'amount': 20,
                    'external_id': self.oid, 'status': 'pending', 'pix_copia_cola': '000201-test',
                    'qr_code_base64': 'data:image/png;base64,test', 'expires_at': '2030-01-01T00:00:00Z'}}
        return {'id': 'gateway-123', 'amount': 1 if self.wrong_amount else 20,
                'external_id': self.oid, 'status': self.provider_status}
    def request(self, path, method='GET', payload=None, cookie=None, headers=None):
        route, _, query = path.partition('?')
        body = json.dumps(payload or {}).encode()
        env = {'REQUEST_METHOD': method, 'PATH_INFO': route, 'QUERY_STRING': query,
               'HTTP_COOKIE': self.cookie if cookie is None else cookie,
               'HTTP_ORIGIN': 'http://localhost:8080', 'HTTP_X_PIX_CLIENT': '1',
               'CONTENT_TYPE': 'application/json', 'CONTENT_LENGTH': str(len(body)),
               'wsgi.input': io.BytesIO(body)}
        env.update(headers or {})
        output = {}
        def respond(status, hdr): output.update(status=int(status.split()[0]), headers=dict(hdr))
        result = b''.join(server.app(env, respond))
        if cookie is None and 'Set-Cookie' in output['headers']:
            self.cookie = output['headers']['Set-Cookie'].split(';')[0]
        output['body'] = json.loads(result) if output['headers'].get('Content-Type', '').startswith('application/json') else result
        return output
    def create(self):
        return self.request('/api/pix/criar', 'POST', {'amount': .01, 'videoUrl': 'https://attacker.invalid'})['body']
    def stale(self):
        with server.db() as c: c.execute('UPDATE pix_orders SET checked=0')
    def test_amount_is_fixed_and_payload_matches_documentation(self):
        d = self.create()
        self.assertEqual(d['amount'], 20)
        self.assertEqual(self.calls[0][1]['amount'], 20)
        self.assertEqual(self.calls[0][0], '/api/pix/create')
        self.assertEqual(d['code'], '000201-test')
    def test_pending_then_paid_and_signed_redirect(self):
        d = self.create(); oid = d['id']
        self.assertEqual(self.request('/api/pix/status?id='+oid)['body'], {'status': 'pending'})
        self.assertEqual(self.request('/api/video?id='+oid)['status'], 403)
        self.provider_status = 'paid'; self.stale()
        result = self.request('/api/pix/status?id='+oid)['body']
        self.assertEqual(result['status'], 'paid')
        v = self.request(result['videoUrl'])
        self.assertEqual(v['status'], 302)
        self.assertTrue(v['headers']['Location'].startswith('https://project.supabase.co/'))
        self.assertEqual(v['headers']['Cache-Control'], 'no-store')
    def test_other_browser_cannot_read_order_or_video(self):
        d = self.create()
        for route in ('/api/pix/status', '/api/video'):
            self.assertEqual(self.request(route+'?id='+d['id'], cookie='pix_session='+'b'*43)['status'], 404)
    def test_resume_and_no_duplicate(self):
        d = self.create(); second = self.create()
        self.assertEqual(d['id'], second['id'])
        self.assertEqual(len([p for p in self.calls if p[1]]), 1)
        self.assertEqual(self.request('/api/pix/atual')['body']['id'], d['id'])
    def test_gateway_timeout_retries_same_external_id(self):
        self.failed_create = True
        self.assertEqual(self.request('/api/pix/criar','POST')['status'], 502)
        oid = self.oid; self.failed_create = False
        self.assertEqual(self.create()['id'], oid)
    def test_mismatched_amount_never_unlocks(self):
        d = self.create(); self.provider_status = 'paid'; self.wrong_amount = True
        self.assertEqual(self.request('/api/pix/status?id='+d['id'])['status'], 502)
        self.assertEqual(self.request('/api/video?id='+d['id'])['status'], 502)
    def test_expired_and_cancelled(self):
        for state in ('expired', 'cancelled'):
            d = self.create(); self.provider_status = state; self.stale()
            self.assertEqual(self.request('/api/pix/status?id='+d['id'])['body']['status'], state)
            self.assertNotEqual(self.create()['id'], d['id'])
    def test_origin_protection(self):
        r = self.request('/api/pix/criar','POST',headers={'HTTP_ORIGIN':'https://evil.invalid'})
        self.assertEqual(r['status'], 403); self.assertFalse(self.calls)
    def test_missing_video_prevents_charge(self):
        self.storage_ok = False
        self.assertEqual(self.request('/api/pix/criar','POST')['status'], 503)
        self.assertFalse(self.calls)
    def test_secrets_and_private_files_are_not_public(self):
        for path in ('/.env','/server.py','/data/orders.sqlite3','/private/completo.mp4','/../server.py'):
            self.assertEqual(self.request(path)['status'], 404)
    def test_paid_status_survives_new_connection(self):
        d = self.create(); self.provider_status = 'paid'
        self.request('/api/pix/status?id='+d['id'])
        with server.db() as conn:
            self.assertEqual(conn.execute('SELECT status FROM pix_orders').fetchone()[0], 'paid')

class StorageSecurity(unittest.TestCase):
    def test_private_bucket_produces_signed_link(self):
        from unittest.mock import MagicMock
        c = MagicMock(); c.storage.get_bucket.return_value = {'public': False}
        c.storage.from_.return_value.create_signed_url.return_value = {
            'signedURL': 'https://project.supabase.co/storage/v1/object/sign/a?token=t'}
        with patch.object(server, 'SUPABASE_URL', 'https://project.supabase.co'), patch.object(server, 'storage_client', return_value=c):
            self.assertIn('token=t', server.signed_video_url())
            c.storage.from_.return_value.create_signed_url.assert_called_once_with(server.VIDEO_PATH, 3600)
    def test_public_bucket_is_rejected(self):
        from unittest.mock import MagicMock
        c = MagicMock(); c.storage.get_bucket.return_value = {'public': True}
        with patch.object(server, 'storage_client', return_value=c):
            with self.assertRaises(server.Failure): server.signed_video_url()
        c.storage.from_.assert_not_called()
    def test_missing_object_is_rejected(self):
        from unittest.mock import MagicMock
        c = MagicMock(); c.storage.get_bucket.return_value = {'public': False}
        c.storage.from_.return_value.create_signed_url.side_effect = ValueError('Object missing')
        with patch.object(server, 'storage_client', return_value=c):
            with self.assertRaises(server.Failure): server.signed_video_url()
    def test_foreign_signed_url_is_rejected(self):
        from unittest.mock import MagicMock
        c = MagicMock(); c.storage.get_bucket.return_value = {'public': False}
        c.storage.from_.return_value.create_signed_url.return_value = {'signedURL': 'https://evil.invalid/file'}
        with patch.object(server, 'SUPABASE_URL', 'https://project.supabase.co'), patch.object(server, 'storage_client', return_value=c):
            with self.assertRaises(server.Failure): server.signed_video_url()

if __name__ == '__main__': unittest.main()
