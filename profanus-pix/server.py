"""Integração ProfanusPay: WSGI, PostgreSQL e Supabase Storage privado. Python 3.11+."""
import json
import mimetypes
import os
import re
import secrets
import psycopg
from psycopg.rows import dict_row
from supabase import create_client
import time
from decimal import Decimal, InvalidOperation
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlsplit
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
DATABASE_URL = os.getenv('DATABASE_URL', '')
SUPABASE_URL = os.getenv('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY = os.getenv('SUPABASE_SECRET_KEY') or os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')
VIDEO_BUCKET = os.getenv('VIDEO_BUCKET', 'videos-pagos')
VIDEO_PATH = os.getenv('VIDEO_PATH', 'completo.mp4')
VIDEO_LINK_SECONDS = int(os.getenv('VIDEO_LINK_SECONDS', '3600'))
AMOUNT = Decimal(os.getenv('PIX_AMOUNT', '20.00'))
if not AMOUNT.is_finite() or AMOUNT < 1 or AMOUNT != AMOUNT.quantize(Decimal('.01')):
    raise ValueError('PIX_AMOUNT deve ser um valor em reais, mínimo 1, com até 2 casas decimais.')
AMOUNT_CENTS = int(AMOUNT * 100)
EXPIRATION = int(os.getenv('PIX_EXPIRATION', '1800'))
COOKIE_SECURE = urlsplit(PUBLIC_URL).scheme == 'https'

class Failure(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message

def database_error_code(error):
    # Classify locally; never log the original exception/URI/password.
    msg = str(error).lower()
    state = getattr(error, 'sqlstate', None)
    if state == '28P01' or 'password authentication failed' in msg:
        return 'DB_PASSWORD', 'Senha do banco incorreta ou mal codificada na URI.'
    if 'circuit breaker' in msg:
        return 'DB_CIRCUIT_BREAKER', 'Pooler bloqueou temporariamente novas conexoes apos falhas de autenticacao.'
    if 'max client connections' in msg or 'too many clients' in msg:
        return 'DB_CONNECTION_LIMIT', 'Limite de conexoes do banco/pooler atingido.'
    if 'connection refused' in msg:
        return 'DB_REFUSED', 'Host/porta recusou a conexao. Confira a URI do pooler e projeto ativo.'
    if 'tenant or user not found' in msg:
        return 'DB_POOLER_USER', 'Usuario/host do pooler incorreto. Copie a URI de Connect > Session pooler.'
    if 'could not translate host' in msg or 'name or service not known' in msg or 'nodename nor servname' in msg:
        return 'DB_HOST', 'Host nao encontrado. Confira a URI do Session pooler.'
    if 'network is unreachable' in msg or 'cannot assign requested address' in msg:
        return 'DB_NETWORK', 'Conexao inacessivel; use Session pooler IPv4 em vez de conexao direta IPv6.'
    if 'timeout' in msg or 'timed out' in msg:
        return 'DB_TIMEOUT', 'Conexao expirou. Confira se o projeto esta ativo, a porta e as restricoes de rede.'
    if 'unsupported startup parameter' in msg or 'invalid startup parameter' in msg:
        return 'DB_STARTUP', 'O pooler recusou um parametro de inicializacao.'
    if 'invalid' in msg and ('uri' in msg or 'connection' in msg or 'integer' in msg):
        return 'DB_URI', 'URI invalida. Confira formato e codificacao dos caracteres da senha.'
    if 'ssl' in msg or 'certificate' in msg:
        return 'DB_SSL', 'Falha SSL na conexao ao banco.'
    if state == '42P01':
        return 'DB_SCHEMA', 'Execute supabase.sql no SQL Editor do projeto correto.'
    if state == '42501':
        return 'DB_PERMISSION', 'A conexao ao banco nao tem a permissao necessaria.'
    return 'DB_CONNECTION', 'Conexao recusada. Confira URI, senha, projeto ativo e restricoes de rede.'

def safe_database_detail(error):
    message = str(error)
    sensitive = [DATABASE_URL, API_KEY, SUPABASE_KEY]
    try:
        from psycopg.conninfo import conninfo_to_dict
        password = conninfo_to_dict(DATABASE_URL).get('password', '')
        sensitive += [password, unquote(password), quote(password, safe='')]
    except Exception:
        pass
    try:
        password = urlsplit(DATABASE_URL).password or ''
        sensitive += [password, unquote(password), quote(unquote(password), safe='')]
    except ValueError:
        pass
    # Handles even an invalid URI with unencoded reserved characters in the password.
    candidate = re.search(r'postgres(?:ql)?://[^:]+:(.*)@', DATABASE_URL)
    if candidate:
        sensitive += [candidate.group(1), unquote(candidate.group(1))]
    for secret in sorted(set(v for v in sensitive if v), key=len, reverse=True):
        message = message.replace(secret, '[REDACTED]')
    message = re.sub(r'(?:postgres(?:ql)?|https?)://[^\s\"\']+', '[URI_REDACTED]', message)
    message = re.sub(r"(?i)(password\s*=\s*)(?:'[^']*'|\"[^\"]*\"|[^\s;]+)", r'\1[REDACTED]', message)
    message = ' | '.join(line.strip() for line in message.splitlines() if line.strip())
    return message[:1200]

def report_database_error(error):
    code, explanation = database_error_code(error)
    print('SUPABASE_DB_ERROR [' + code + '] ' + explanation, flush=True)
    print('SUPABASE_DB_DETAIL ' + safe_database_detail(error), flush=True)
    return code

def db():
    if not DATABASE_URL:
        raise Failure(503, 'Configure DATABASE_URL do Supabase no servidor.')
    conn = None
    try:
        # Supabase pooler may reject libpq startup "options". Configure after connecting.
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10,
                               sslmode='require', prepare_threshold=None)
        conn.execute("SET statement_timeout = '30s'")
        conn.execute("SET lock_timeout = '25s'")
        conn.commit()
        return conn
    except psycopg.Error as error:
        if conn is not None: conn.close()
        report_database_error(error)
        raise Failure(503, 'Banco de dados indisponível. Confira a configuração do Supabase.')

def storage_client():
    if not SUPABASE_URL.startswith('https://') or not SUPABASE_KEY:
        raise Failure(503, 'Configure a URL e a chave secreta do Supabase no servidor.')
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def signed_video_url(seconds=None):
    try:
        client = storage_client()
        bucket = client.storage.get_bucket(VIDEO_BUCKET)
        is_public = bucket.get('public') if isinstance(bucket, dict) else bucket.public
        if is_public:
            raise Failure(503, 'O bucket do vídeo deve ser privado. Nenhum acesso foi liberado.')
        response = client.storage.from_(VIDEO_BUCKET).create_signed_url(
            VIDEO_PATH, seconds or VIDEO_LINK_SECONDS)
        url = response.get('signedURL') or response.get('signedUrl')
        if not isinstance(url, str) or urlsplit(url).netloc != urlsplit(SUPABASE_URL).netloc or not url.startswith('https://'):
            raise ValueError('Invalid storage URL')
        return url
    except Failure:
        raise
    except Exception:
        raise Failure(503, 'Vídeo indisponível. Confira o bucket privado, o arquivo e a chave do Supabase.')

def lock_session(conn, session):
    # Transaction-scoped lock works across workers and survives connection pooling.
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', (session,))

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

def refresh(conn, order, commit=True):
    # Short cache limits concurrent tab polling; first confirmation always comes from API.
    if time.time() - order['checked'] < 3:
        return order
    tx = checked_transaction(gateway('/api/pix/' + quote(order['gateway_id'], safe='')), order)
    conn.execute('UPDATE pix_orders SET status=%s, checked=%s WHERE id=%s',
                 (tx['status'], time.time(), order['id']))
    if commit: conn.commit()
    return conn.execute('SELECT * FROM pix_orders WHERE id=%s', (order['id'],)).fetchone()

def public_order(order):
    return {'id': order['id'], 'code': order['code'], 'qr': order['qr'],
            'status': order['status'], 'expiresAt': order['expires'], 'amount': order['amount'] / 100}

def create_order(session):
    with db() as conn:
        lock_session(conn, session)
        order = conn.execute('SELECT * FROM pix_orders WHERE session=%s ORDER BY created DESC LIMIT 1', (session,)).fetchone()
        if order and order['gateway_id']:
            order = refresh(conn, order, commit=False)
            if order['status'] in ('pending', 'paid'):
                return public_order(order)
        # Validate object existence and private bucket BEFORE any new payment.
        signed_video_url(60)
        if not order or order['status'] in ('expired', 'cancelled'):
            oid = 'video-' + secrets.token_urlsafe(24)
            conn.execute('INSERT INTO pix_orders(id,session,amount,created) VALUES(%s,%s,%s,%s)',
                         (oid, session, AMOUNT_CENTS, time.time()))
            # Persist external_id before network call so timeout cannot duplicate a charge.
            conn.commit()
            lock_session(conn, session)
            order = conn.execute('SELECT * FROM pix_orders WHERE id=%s', (oid,)).fetchone()
            if order['gateway_id']:
                return public_order(order)
        payload = {'amount': order['amount'] / 100, 'description': 'Acesso ao vídeo completo',
                   'external_id': order['id'], 'expiration': EXPIRATION}
        result = gateway('/api/pix/create', payload)
        tx = result.get('transaction')
        if not isinstance(tx, dict) or not tx.get('id') or not tx.get('pix_copia_cola'):
            raise Failure(502, 'O gateway não retornou o código Pix. Tente novamente.')
        trial = dict(order); trial['gateway_id'] = tx['id']
        checked_transaction(tx, trial)
        conn.execute('UPDATE pix_orders SET gateway_id=%s,code=%s,qr=%s,status=%s,expires=%s,checked=0 WHERE id=%s',
                     (tx['id'], tx['pix_copia_cola'], tx.get('qr_code_base64', ''), tx['status'], tx.get('expires_at'), order['id']))
        conn.commit()
        return public_order(conn.execute('SELECT * FROM pix_orders WHERE id=%s', (order['id'],)).fetchone())

def owned_order(conn, oid, session):
    order = conn.execute('SELECT * FROM pix_orders WHERE id=%s AND session=%s', (oid, session)).fetchone()
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
        elif path == '/health':
            data = {'ok': True}
        elif path == '/api/config':
            data = {'amount': float(AMOUNT)}
        elif path == '/api/pix/atual':
            with db() as conn:
                order = conn.execute('SELECT * FROM pix_orders WHERE session=%s ORDER BY created DESC LIMIT 1', (session,)).fetchone()
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
            # Signed URL is issued only after server-side gateway confirmation.
            url = signed_video_url()
            start_response('302 Found', headers + [('Location', url), ('Content-Length', '0')])
            return []
        elif path in ('/', '/index.html', '/perfil.jpg', '/video.mp4'):
            name = 'index.html' if path == '/' else path[1:]
            return file_response(ROOT / 'public' / name, environ, start_response, headers)
        else:
            raise Failure(404, 'Rota não encontrada.')
    except Failure as e:
        status, data = f'{e.status} Error', {'error': e.message}
    except psycopg.Error as error:
        report_database_error(error)
        status, data = '503 Service Unavailable', {'error': 'Banco indisponível. Confira a conexão e execute supabase.sql.'}
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
