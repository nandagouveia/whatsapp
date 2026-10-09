"""Integração ProfanusPay: WSGI, SQLite e vídeo privado. Python 3.11+."""
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen
from wsgiref.simple_server import make_server

ROOT = Path(__file__).resolve().parent
# .env is local configuration only; never served over HTTP.
if (ROOT / '.env').exists():
    for line in (ROOT / '.env').read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('\"').strip("'"))
API_BASE = 'https://nexuspag.com'
API_KEY = os.getenv('PROFANUS_API_KEY', '')
PUBLIC_URL = os.getenv('PUBLIC_URL', 'http://localhost:8080').rstrip('/')
DB_PATH = os.getenv('DATABASE_PATH', str(ROOT / 'data' / 'orders.sqlite3'))
VIDEO = Path(os.getenv('FULL_VIDEO_PATH', str(ROOT / 'private' / 'completo.mp4'))).resolve()
AMOUNT = Decimal(os.getenv('PIX_AMOUNT', '20.00'))
if not AMOUNT.is_finite() or AMOUNT < 1 or AMOUNT != AMOUNT.quantize(Decimal('.01')):
    raise ValueError('PIX_AMOUNT deve ser um valor em reais, mínimo 1, com até 2 casas decimais.')
AMOUNT_CENTS = int(AMOUNT * 100)
EXPIRATION = int(os.getenv('PIX_EXPIRATION', '1800'))
COOKIE_SECURE = urlsplit(PUBLIC_URL).scheme == 'https'

class Failure(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message

def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=35)
    conn.row_factory = sqlite3.Row
    conn.execute('''CREATE TABLE IF NOT EXISTS orders (
        id TEXT PRIMARY KEY, session TEXT NOT NULL, amount INTEGER NOT NULL,
        gateway_id TEXT, code TEXT, qr TEXT, status TEXT NOT NULL DEFAULT 'creating',
        expires TEXT, created REAL NOT NULL, checked REAL NOT NULL DEFAULT 0)''')
    conn.commit()
    return conn

def gateway(path, payload=None):
    if not API_KEY:
        raise Failure(503, 'Configure a chave da ProfanusPay no servidor.')
    request = Request(API_BASE + path,
                      data=json.dumps(payload).encode() if payload is not None else None,
                      headers={'x-api-key': API_KEY, 'Content-Type': 'application/json'},
                      method='POST' if payload is not None else 'GET')
    try:
        with urlopen(request, timeout=20) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get('success') is False:
            raise Failure(502, 'O gateway não retornou uma cobrança válida.')
        return result
    except HTTPError as e:
        if e.code == 401:
            raise Failure(503, 'Credencial do gateway inválida. Contate o responsável pela página.')
        if e.code == 429:
            raise Failure(429, 'Gateway ocupado. Aguarde alguns segundos e tente novamente.')
        raise Failure(502, 'Não foi possível concluir a consulta ao gateway. Tente novamente.')
    except (URLError, TimeoutError, ValueError, OSError):
        raise Failure(502, 'Gateway temporariamente indisponível. Tente novamente.')

def checked_transaction(data, order):
    tx = data.get('transaction', data)
    try:
        cents = Decimal(str(tx['amount'])) * 100
        valid = cents.is_finite() and cents == order['amount']
        valid = valid and tx['id'] == order['gateway_id']
        if tx.get('external_id') is not None:
            valid = valid and tx['external_id'] == order['id']
        if not valid or tx['status'] not in ('pending', 'paid', 'expired', 'cancelled'):
            raise ValueError()
        return tx
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise Failure(502, 'Dados da cobrança inconsistentes. O acesso não foi liberado.')

def refresh(conn, order):
    # Short cache limits concurrent tab polling; first confirmation always comes from API.
    if time.time() - order['checked'] < 3:
        return order
    tx = checked_transaction(gateway('/api/pix/' + quote(order['gateway_id'], safe='')), order)
    conn.execute('UPDATE orders SET status=?, checked=? WHERE id=?',
                 (tx['status'], time.time(), order['id']))
    conn.commit()
    return conn.execute('SELECT * FROM orders WHERE id=?', (order['id'],)).fetchone()

def public_order(order):
    return {'id': order['id'], 'code': order['code'], 'qr': order['qr'],
            'status': order['status'], 'expiresAt': order['expires'], 'amount': order['amount'] / 100}

def create_order(session):
    if not VIDEO.is_file():
        raise Failure(503, 'O vídeo completo ainda não foi configurado. Nenhuma cobrança foi criada.')
    with db() as conn:
        # Serialize creation across workers; stable external_id survives API timeout/retry.
        conn.execute('BEGIN IMMEDIATE')
        order = conn.execute('SELECT * FROM orders WHERE session=? ORDER BY created DESC LIMIT 1', (session,)).fetchone()
        if order and order['gateway_id']:
            order = refresh(conn, order)
            if not conn.in_transaction:
                conn.execute('BEGIN IMMEDIATE')
            order = conn.execute('SELECT * FROM orders WHERE session=? ORDER BY created DESC LIMIT 1', (session,)).fetchone()
            if order['status'] in ('pending', 'paid'):
                return public_order(order)
        if not order or order['status'] in ('expired', 'cancelled'):
            oid = 'video-' + secrets.token_urlsafe(24)
            conn.execute('INSERT INTO orders(id,session,amount,created) VALUES(?,?,?,?)',
                         (oid, session, AMOUNT_CENTS, time.time()))
            conn.commit()
            conn.execute('BEGIN IMMEDIATE')
            order = conn.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
        payload = {'amount': order['amount'] / 100, 'description': 'Acesso ao vídeo completo',
                   'external_id': order['id'], 'expiration': EXPIRATION}
        result = gateway('/api/pix/create', payload)
        tx = result.get('transaction')
        if not isinstance(tx, dict) or not tx.get('id') or not tx.get('pix_copia_cola'):
            raise Failure(502, 'O gateway não retornou o código Pix. Tente novamente.')
        trial = dict(order); trial['gateway_id'] = tx['id']
        checked_transaction(tx, trial)
        conn.execute('UPDATE orders SET gateway_id=?,code=?,qr=?,status=?,expires=?,checked=0 WHERE id=?',
                     (tx['id'], tx['pix_copia_cola'], tx.get('qr_code_base64', ''), tx['status'], tx.get('expires_at'), order['id']))
        conn.commit()
        return public_order(conn.execute('SELECT * FROM orders WHERE id=?', (order['id'],)).fetchone())

def owned_order(conn, oid, session):
    order = conn.execute('SELECT * FROM orders WHERE id=? AND session=?', (oid, session)).fetchone()
    if not order or not order['gateway_id']:
        raise Failure(404, 'Cobrança não encontrada neste navegador.')
    return order

def file_response(path, environ, start_response, headers, private=False):
    if not path.is_file():
        raise Failure(404, 'Arquivo não encontrado.')
    size = path.stat().st_size
    start, end, status = 0, size - 1, '200 OK'
    rng = environ.get('HTTP_RANGE', '') if private else ''
    if rng:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', rng)
        if not match or not any(match.groups()):
            start_response('416 Range Not Satisfiable', headers + [('Content-Range', f'bytes */{size}')])
            return []
        a, b = match.groups()
        if not a:
            start = max(0, size - int(b))
        else:
            start, end = int(a), min(size - 1, int(b)) if b else size - 1
        if start > end or start >= size:
            start_response('416 Range Not Satisfiable', headers + [('Content-Range', f'bytes */{size}')])
            return []
        status = '206 Partial Content'
        headers += [('Content-Range', f'bytes {start}-{end}/{size}')]
    length = max(0, end - start + 1)
    headers += [('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream'),
                ('Content-Length', str(length)), ('Accept-Ranges', 'bytes')]
    start_response(status, headers)
    if environ['REQUEST_METHOD'] == 'HEAD':
        return []
    def stream():
        with path.open('rb') as f:
            f.seek(start)
            remaining = length
            while remaining:
                chunk = f.read(min(65536, remaining))
                if not chunk: break
                remaining -= len(chunk)
                yield chunk
    return stream()

def app(environ, start_response):
    cookie = SimpleCookie()
    try: cookie.load(environ.get('HTTP_COOKIE', ''))
    except Exception: pass
    session = cookie['pix_session'].value if 'pix_session' in cookie else ''
    new_session = not re.fullmatch(r'[A-Za-z0-9_-]{43}', session)
    if new_session: session = secrets.token_urlsafe(32)
    headers = [('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
               ('Referrer-Policy', 'same-origin'), ('X-Frame-Options', 'DENY')]
    if new_session:
        headers.append(('Set-Cookie', f'pix_session={session}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000' + ('; Secure' if COOKIE_SECURE else '')))
    path, method = environ.get('PATH_INFO', '/'), environ['REQUEST_METHOD']
    query = parse_qs(environ.get('QUERY_STRING', ''))
    status = '200 OK'
    try:
        if method not in ('GET', 'HEAD', 'POST'):
            raise Failure(405, 'Método não permitido.')
        if method == 'POST':
            if environ.get('HTTP_X_PIX_CLIENT') != '1' or environ.get('HTTP_ORIGIN') != PUBLIC_URL:
                raise Failure(403, 'Origem da requisição inválida.')
            if path != '/api/pix/criar': raise Failure(404, 'Rota não encontrada.')
            if environ.get('CONTENT_TYPE', '').split(';')[0] != 'application/json':
                raise Failure(415, 'Envie JSON.')
            if int(environ.get('CONTENT_LENGTH') or 0) > 2048:
                raise Failure(413, 'Requisição muito grande.')
            # Never read amount or video URL from browser.
            data = create_order(session)
        elif path == '/api/config':
            data = {'amount': float(AMOUNT)}
        elif path == '/api/pix/atual':
            with db() as conn:
                order = conn.execute('SELECT * FROM orders WHERE session=? ORDER BY created DESC LIMIT 1', (session,)).fetchone()
                data = public_order(order) if order and order['gateway_id'] else {}
        elif path == '/api/pix/status':
            with db() as conn:
                order = refresh(conn, owned_order(conn, query.get('id', [''])[0], session))
                data = {'status': order['status']}
                if order['status'] == 'paid':
                    data['videoUrl'] = '/api/video?id=' + quote(order['id'])
        elif path == '/api/video':
            with db() as conn:
                order = refresh(conn, owned_order(conn, query.get('id', [''])[0], session))
                if order['status'] != 'paid': raise Failure(403, 'Pagamento ainda não confirmado.')
            return file_response(VIDEO, environ, start_response, headers, private=True)
        elif path in ('/', '/index.html', '/perfil.jpg', '/video.mp4'):
            name = 'index.html' if path == '/' else path[1:]
            return file_response(ROOT / 'public' / name, environ, start_response, headers)
        else:
            raise Failure(404, 'Rota não encontrada.')
    except Failure as e:
        status, data = f'{e.status} Error', {'error': e.message}
    except Exception:
        # Do not print provider payload, payer information or credentials.
        status, data = '500 Internal Server Error', {'error': 'Erro interno. Tente novamente.'}
    body = json.dumps(data, ensure_ascii=False).encode()
    start_response(status, headers + [('Content-Type', 'application/json; charset=utf-8'), ('Content-Length', str(len(body)))])
    return [] if method == 'HEAD' else [body]

if __name__ == '__main__':
    port = int(os.getenv('PORT', '8080'))
    print(f'Acesse http://localhost:{port} — servidor local de desenvolvimento')
    make_server('127.0.0.1', port, app).serve_forever()
