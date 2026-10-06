"""Administrator-only, separate-origin bridge to one fixed local SnowLuma server.

No credentials are loaded here. SnowLuma keeps its own login and authorization.
The bridge can be served locally, or dispatched by the panel through a restricted
SSH forward whose destination remains the panel's existing listening port.
"""
import base64
import http.client
import json
import queue
import secrets
import socket
import threading
import time
import zlib
from http.cookies import CookieError, SimpleCookie
from urllib.parse import unquote, urlsplit

import panel_endpoint
BRIDGE_PORT = panel_endpoint.BRIDGE_PORT
BRIDGE_HOST = f'127.0.0.1:{BRIDGE_PORT}'
BRIDGE_ORIGIN = panel_endpoint.BRIDGE_ORIGIN
PARENT_ORIGIN = panel_endpoint.ORIGIN
UPSTREAM_HOST = '127.0.0.1'
UPSTREAM_PORT = 5099
UPSTREAM_ORIGIN = 'http://127.0.0.1:5099'
AVATAR_COOKIE = 'snowluma_avatar_session'
CLIENT_AVATAR_COOKIE = 'snowluma_avatar_session_local' if panel_endpoint.PROFILE=='local' else AVATAR_COOKIE
CONNECT_TIMEOUT = 2.0
RESPONSE_TIMEOUT = 10.0
STREAM_IDLE_TIMEOUT = 300.0
AUTH_CHECK_INTERVAL = 1.0
MAX_BODY = 4 * 1024 * 1024
MAX_HTML = 8 * 1024 * 1024
MAX_PATH = 8192
MAX_STREAMS = 4
STREAM_QUEUE_SIZE = 8
CHUNK_SIZE = 32768
HEALTH_CACHE_SECONDS = 5.0

_lock = threading.Lock()
_streams = {}
_health = {'available': False, 'state': 'unchecked', 'checked_at': None,
           'upstream_status': None}
_health_checked = 0.0
_REQUEST_HEADERS = {'accept', 'accept-language', 'content-type', 'range',
                    'if-none-match', 'if-modified-since', 'last-event-id'}
_RESPONSE_HEADERS = {'content-type', 'content-encoding', 'content-language',
                     'content-disposition', 'accept-ranges', 'content-range',
                     'etag', 'last-modified', 'retry-after', 'www-authenticate'}
_METHODS = {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'}


class _Connection(http.client.HTTPConnection):
    """Retain the socket even when HTTPConnection hands ownership to a response."""
    def connect(self):
        super().connect()
        self.bridge_socket = self.sock


def _upstream_connection():
    return _Connection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=CONNECT_TIMEOUT)


def is_bridge_host(handler):
    values = handler.headers.get_all('Host', [])
    return len(values) == 1 and values[0] == BRIDGE_HOST


def _safe_header(value):
    return isinstance(value, str) and len(value) <= 16384 and not any(
        ord(c) < 32 or ord(c) == 127 for c in value)


def _session(access, cookie):
    try:
        return access.session(cookie)
    except Exception:
        # Invalid/unreadable account state never grants access.
        return None


def _authorized(access, cookie, key):
    current = _session(access, cookie)
    return bool(current and current.get('role') == 'admin' and
                current.get('key') == key)


def _remember(available, upstream_status=None):
    global _health_checked
    with _lock:
        _health.update(available=bool(available),
                       state='ready' if available else 'offline',
                       checked_at=time.time(), upstream_status=upstream_status)
        _health_checked = time.monotonic()


def status():
    """Return bounded, credential-free availability metadata; never read a body."""
    with _lock:
        cached = time.monotonic() - _health_checked < HEALTH_CACHE_SECONDS
    if not cached:
        connection = _upstream_connection()
        try:
            connection.request('HEAD', '/', headers={'Host': '127.0.0.1:5099',
                               'Connection': 'close', 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            _remember(True, response.status)
            response.close()
        except (OSError, http.client.HTTPException, ValueError):
            _remember(False)
        finally:
            _abort(connection)
    with _lock:
        return {**_health, 'bridge_origin': BRIDGE_ORIGIN,
                'max_streams_per_session': MAX_STREAMS}


def _abort(connection):
    # shutdown interrupts a reader waiting on an idle SSE socket. Calling only
    # HTTPResponse.close() can wait on the buffered reader's lock indefinitely.
    sock = getattr(connection, 'bridge_socket', None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    try:
        connection.close()
    except OSError:
        pass


def _path(target):
    if not isinstance(target, str) or len(target) > MAX_PATH:
        raise ValueError('path')
    if not target.startswith('/') or target.startswith('//'):
        raise ValueError('path')
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ValueError('path')
    decoded = target
    for _ in range(5):
        if '\\' in decoded or any(ord(c) < 32 or ord(c) == 127 for c in decoded):
            raise ValueError('path')
        path_only = decoded.split('?', 1)[0]
        if path_only.startswith('//') or any(p in ('.', '..') for p in path_only.split('/')):
            raise ValueError('path')
        next_value = unquote(decoded, errors='strict')
        if next_value == decoded:
            return target
        decoded = next_value
    # Reject endlessly encoded paths rather than depend on upstream decoding.
    raise ValueError('path')


def _same_origin_url(value):
    if not _safe_header(value):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == 'http' and parsed.netloc == BRIDGE_HOST and
                parsed.username is None and parsed.password is None)
    except ValueError:
        return False


def _api_path(target):
    # Encoded slashes and a query on /api must not bypass the origin gate.
    decoded = target
    for _ in range(5):
        next_value = unquote(decoded, errors='strict')
        if next_value == decoded:
            break
        decoded = next_value
    path = decoded.split('?', 1)[0].lower()
    return path == '/api' or path.startswith('/api/')


def _request_target(handler):
    # BaseHTTPRequestHandler normalizes a leading // before do_GET. Inspect the
    # original request target as well so a protocol-relative URL is rejected.
    requestline = getattr(handler, 'requestline', '')
    words = requestline.split() if isinstance(requestline, str) else []
    return words[1] if len(words) in (2, 3) else handler.path


def _api_origin(handler):
    origins = handler.headers.get_all('Origin', [])
    if origins:
        return len(origins) == 1 and origins[0] == BRIDGE_ORIGIN
    if handler.command != 'GET':
        return False
    referers = handler.headers.get_all('Referer', [])
    if referers:
        return len(referers) == 1 and _same_origin_url(referers[0])
    return handler.headers.get_all('Sec-Fetch-Site', []) == ['same-origin']


def _cookies(cookie):
    if not _safe_header(cookie):
        return ''
    try:
        parsed = SimpleCookie()
        parsed.load(cookie)
        item = parsed.get(CLIENT_AVATAR_COOKIE)
        if not item:return ''
        upstream=SimpleCookie();upstream[AVATAR_COOKIE]=item.value
        return upstream[AVATAR_COOKIE].OutputString(attrs=[])
    except CookieError:
        return ''


def _set_cookie(value):
    if not _safe_header(value):
        return None
    try:
        parsed = SimpleCookie()
        parsed.load(value)
        if set(parsed) != {AVATAR_COOKIE}:
            return None
        item = parsed[AVATAR_COOKIE]
        if CLIENT_AVATAR_COOKIE!=AVATAR_COOKIE:
            browser=SimpleCookie();browser[CLIENT_AVATAR_COOKIE]=item.value
            for attribute,setting in item.items():browser[CLIENT_AVATAR_COOKIE][attribute]=setting
            item=browser[CLIENT_AVATAR_COOKIE]
        item['domain'] = ''
        item['path'] = '/'
        item['samesite'] = 'Strict'
        return item.OutputString()
    except CookieError:
        return None


def _upstream_headers(handler, body_length):
    headers = {'Host': '127.0.0.1:5099', 'Origin': UPSTREAM_ORIGIN,
               'Referer': UPSTREAM_ORIGIN + '/', 'Accept-Encoding': 'identity',
               'Connection': 'close'}
    for name, value in handler.headers.items():
        if name.lower() in _REQUEST_HEADERS and _safe_header(value):
            headers[name] = value
    authorization = handler.headers.get_all('Authorization', [])
    if len(authorization) == 1 and _safe_header(authorization[0]):
        if authorization[0].startswith('Bearer '):
            headers['Authorization'] = authorization[0]
    avatar = _cookies(handler.headers.get('Cookie', ''))
    if avatar:
        headers['Cookie'] = avatar
    if body_length or handler.command in ('POST', 'PUT', 'PATCH', 'DELETE'):
        headers['Content-Length'] = str(body_length)
    return headers


def _csp(policies, nonce=None):
    def with_nonce(sources):
        # A new nonce disables CSP's existing unsafe-inline allowance. Avoid
        # changing SnowLuma's working inline scripts when that allowance alone
        # already permits our small notification script.
        explicit = any(s.startswith(("'nonce-", "'sha256-", "'sha384-", "'sha512-"))
                       for s in sources) or "'strict-dynamic'" in sources
        if "'unsafe-inline'" in sources and not explicit:
            return sources
        return [p for p in sources if p != "'none'"] + ["'nonce-" + nonce + "'"]

    output = []
    for policy in policies:
        directives = []
        has_script = False
        default = None
        for raw in policy.split(';'):
            parts = raw.strip().split()
            if not parts or parts[0].lower() == 'frame-ancestors':
                continue
            name = parts[0].lower()
            if name == 'default-src':
                default = parts[1:]
            if nonce and name in ('script-src', 'script-src-elem'):
                parts = [parts[0]] + with_nonce(parts[1:])
                if name == 'script-src':
                    has_script = True
            directives.append(' '.join(parts))
        if nonce and default is not None and not has_script:
            directives.append('script-src ' + ' '.join(with_nonce(default)))
        directives.append('frame-ancestors ' + PARENT_ORIGIN)
        output.append('; '.join(directives))
    return output or ['frame-ancestors ' + PARENT_ORIGIN]


def _begin(handler, code, headers=(), length=None, nonce=None):
    handler.close_connection = True
    # send_response() logs the request URL, which could contain a Snow token.
    # This bridge deliberately emits neither URL nor header/body logs.
    handler.send_response_only(code)
    handler.send_header('Date', handler.date_time_string())
    policies = []
    for name, value in headers:
        lower = name.lower()
        if not _safe_header(value):
            continue
        if lower == 'content-security-policy':
            policies.append(value)
        elif lower == 'set-cookie':
            cookie = _set_cookie(value)
            if cookie:
                handler.send_header('Set-Cookie', cookie)
        elif lower in _RESPONSE_HEADERS:
            handler.send_header(name, value)
    for policy in _csp(policies, nonce):
        handler.send_header('Content-Security-Policy', policy)
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('Pragma', 'no-cache')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('Referrer-Policy', 'same-origin')
    handler.send_header('Connection', 'close')
    if length is not None:
        handler.send_header('Content-Length', str(length))
    handler.end_headers()


def _script(state, nonce):
    payload = json.dumps({'source': 'nue-snowluma-bridge', 'status': state}, separators=(',', ':'))
    target = json.dumps(PARENT_ORIGIN)
    return ('<script nonce="' + nonce + '">window.parent.postMessage(' +
            payload + ',' + target + ');</script>').encode('ascii')


def _error(handler, code, state='forbidden', api=False, message=None):
    messages = {'login_required': '请先登录主管理员账号。',
                'forbidden': '此页面仅允许主管理员访问。',
                'offline': 'SnowLuma 当前不可用，请在托管机本地检查服务。'}
    if api:
        data = json.dumps({'error': message or messages.get(state, messages['forbidden']),
                           'status': state}, ensure_ascii=False).encode('utf-8')
        _begin(handler, code, [('Content-Type', 'application/json; charset=utf-8')], len(data))
    else:
        nonce = base64.b64encode(secrets.token_bytes(18)).decode('ascii')
        data = ('<!doctype html><meta charset="utf-8"><title>SnowLuma</title><p>' +
                messages.get(state, messages['forbidden']) + '</p>').encode('utf-8') + _script(state, nonce)
        _begin(handler, code, [('Content-Type', 'text/html; charset=utf-8'),
                             ('Content-Security-Policy', "default-src 'none'")], len(data), nonce)
    if handler.command != 'HEAD':
        handler.wfile.write(data)


def _body(handler):
    if handler.headers.get_all('Transfer-Encoding', []):
        raise ValueError('body')
    lengths = handler.headers.get_all('Content-Length', [])
    if len(lengths) > 1:
        raise ValueError('body')
    if not lengths:
        return b''
    if not lengths[0].isascii() or not lengths[0].isdigit():
        raise ValueError('body')
    length = int(lengths[0])
    if length > MAX_BODY:
        raise OverflowError('body')
    timeout = handler.connection.gettimeout()
    try:
        handler.connection.settimeout(RESPONSE_TIMEOUT)
        data = handler.rfile.read(length)
    finally:
        handler.connection.settimeout(timeout)
    if len(data) != length:
        raise ValueError('body')
    return data


def _html(response):
    raw = response.read(MAX_HTML + 1)
    if len(raw) > MAX_HTML:
        raise ValueError('html')
    encoding = response.getheader('Content-Encoding', '').lower()
    if encoding in ('gzip', 'deflate'):
        decoder = zlib.decompressobj(31 if encoding == 'gzip' else zlib.MAX_WBITS)
        raw = decoder.decompress(raw, MAX_HTML + 1)
        if len(raw) > MAX_HTML or not decoder.eof:
            raise ValueError('html')
    elif encoding not in ('', 'identity'):
        raise ValueError('html')
    nonce = base64.b64encode(secrets.token_bytes(18)).decode('ascii')
    snippet = _script('ready', nonce)
    lower = raw.lower()
    position = lower.find(b'<head')
    if position != -1:
        position = raw.find(b'>', position)
        if position != -1:
            position += 1
            return raw[:position] + snippet + raw[position:], nonce
    return snippet + raw, nonce


def _reserve_stream(key):
    with _lock:
        count = _streams.get(key, 0)
        if count >= MAX_STREAMS:
            return False
        _streams[key] = count + 1
        return True


def _release_stream(key):
    with _lock:
        count = _streams.get(key, 0)
        if count <= 1:
            _streams.pop(key, None)
        else:
            _streams[key] = count - 1


def _stream(handler, access, cookie, key, connection, response):
    pending = queue.Queue(STREAM_QUEUE_SIZE)
    stop = threading.Event()

    def put(value):
        while not stop.is_set():
            try:
                pending.put(value, timeout=0.2)
                return
            except queue.Full:
                pass

    def read():
        try:
            while not stop.is_set():
                chunk = response.read1(CHUNK_SIZE)
                if not chunk:
                    break
                put(chunk)
        except (OSError, http.client.HTTPException, ValueError):
            pass
        finally:
            put(None)

    connection.bridge_socket.settimeout(STREAM_IDLE_TIMEOUT)
    reader = threading.Thread(target=read, daemon=True, name='snowluma-bridge-stream')
    reader.start()
    timeout = handler.connection.gettimeout()
    handler.connection.settimeout(AUTH_CHECK_INTERVAL)
    try:
        next_check = time.monotonic()
        while True:
            now = time.monotonic()
            if now >= next_check:
                if not _authorized(access, cookie, key):
                    break
                next_check = now + AUTH_CHECK_INTERVAL
            try:
                chunk = pending.get(timeout=max(0.001, next_check - time.monotonic()))
            except queue.Empty:
                # A readable client socket with an empty peek means it closed.
                # No heartbeat is injected into SnowLuma's stream protocol.
                import select
                if select.select([handler.connection], [], [], 0)[0]:
                    if not handler.connection.recv(1, socket.MSG_PEEK):
                        break
                continue
            if chunk is None:
                break
            handler.wfile.write(chunk)
            handler.wfile.flush()
    finally:
        stop.set()
        _abort(connection)
        reader.join(timeout=AUTH_CHECK_INTERVAL + 0.5)
        handler.connection.settimeout(timeout)


def handle(handler, access):
    """Handle one bridge-origin request; never delegate to the panel handler."""
    response = None
    connection = None
    stream_key = None
    started = False
    try:
        api = _api_path(_request_target(handler))
    except (ValueError, UnicodeError):
        api = True
    try:
        cookie = handler.headers.get('Cookie', '')
        session = _session(access, cookie)
        if not session:
            return _error(handler, 401, 'login_required', api)
        if session.get('role') != 'admin' or not session.get('key'):
            return _error(handler, 403, 'forbidden', api)
        if not is_bridge_host(handler):
            return _error(handler, 403, 'forbidden', api)
        target = _path(_request_target(handler))
        if handler.command not in _METHODS or handler.headers.get('Upgrade'):
            return _error(handler, 405, 'forbidden', api)
        # Protect every write, including any future non-/api SnowLuma endpoint.
        if (api or handler.command not in ('GET', 'HEAD')) and not _api_origin(handler):
            return _error(handler, 403, 'forbidden', api)
        body = _body(handler)
        key = session['key']
        if not _authorized(access, cookie, key):
            return _error(handler, 401, 'login_required', api)
        connection = _upstream_connection()
        connection.request(handler.command, target, body=body,
                           headers=_upstream_headers(handler, len(body)))
        connection.bridge_socket.settimeout(RESPONSE_TIMEOUT)
        response = connection.getresponse()
        _remember(True, response.status)
        if (response.status < 200 or
                300 <= response.status < 400 and response.status != 304 or
                response.status >= 400):
            # Do not expose upstream exception text, redirect targets or tokens.
            if api and response.status in (401, 403):
                message = ('SnowLuma 自身的登录认证未通过，请在其页面中重新登录。'
                           if response.status == 401 else
                           'SnowLuma 拒绝本次操作，请检查其登录和权限。')
                return _error(handler, response.status,
                              'login_required' if response.status == 401 else 'forbidden',
                              True, message)
            return _error(handler, response.status if response.status >= 400 else 502,
                          'offline', api)
        if not _authorized(access, cookie, key):
            return _error(handler, 401, 'login_required', api)
        headers = response.getheaders()
        content_type = response.getheader('Content-Type', '').split(';', 1)[0].lower().strip()
        if content_type == 'text/event-stream' and handler.command != 'HEAD':
            if not _reserve_stream(key):
                return _error(handler, 429, 'forbidden', True)
            stream_key = key
            _begin(handler, response.status, headers)
            started = True
            _stream(handler, access, cookie, key, connection, response)
        elif content_type in ('text/html', 'application/xhtml+xml') and handler.command != 'HEAD':
            data, nonce = _html(response)
            if not _authorized(access, cookie, key):
                return _error(handler, 401, 'login_required', api)
            headers = [(name, value) for name, value in headers
                       if name.lower() not in ('content-encoding', 'etag', 'last-modified')]
            _begin(handler, response.status, headers, len(data), nonce)
            started = True
            handler.wfile.write(data)
        else:
            length = response.length
            _begin(handler, response.status, headers, length)
            started = True
            if handler.command != 'HEAD':
                while True:
                    chunk = response.read1(CHUNK_SIZE)
                    if not chunk:
                        break
                    if not _authorized(access, cookie, key):
                        break
                    handler.wfile.write(chunk)
    except OverflowError:
        if not started:
            _error(handler, 413, 'forbidden', api)
    except (ValueError, UnicodeError):
        if not started:
            _error(handler, 400, 'forbidden', api)
    except (OSError, http.client.HTTPException):
        if response is None:
            _remember(False)
        if not started:
            try:
                _error(handler, 502, 'offline', api)
            except OSError:
                pass
    finally:
        if stream_key is not None:
            _release_stream(stream_key)
        if connection is not None:
            _abort(connection)
        if response is not None:
            try:
                response.close()
            except OSError:
                pass
