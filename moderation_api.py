"""Independent audit configuration and untrusted verdict validation.

This module never calls a provider. Request descriptors are private runtime
objects and must pass through the caller's shared scheduling/accounting layer.
Only public() and save() results may be sent to the panel or settings exports.
"""
import copy
import ipaddress
import json
import math
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
PATH = ROOT / 'moderation-api.json'
DEFAULT = {
    'enabled': False, 'base_url': '', 'model': '', 'timeout_seconds': 30,
    'output_tokens': 512, 'context_messages': 20, 'json_mode': True, 'disable_thinking': True,
    'consent': False,
}
CATEGORIES = frozenset(('targeted_abuse', 'threat', 'harassment', 'spam',
                        'explicit_sexual', 'extreme_gore'))
_TOKEN = re.compile(r'\b(?:sk-[A-Za-z0-9_-]{8,}|sk_[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{8,}|AKIA[A-Z0-9]{16})\b')
_BEARER = re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*')
_SECRET_ASSIGNMENT = re.compile(r'''(?i)(["']?(?:api[_-]?key|access[_-]?token|authorization|password|secret)["']?\s*[:=]\s*["']?)([^\s,"';}\]]{4,})''')


def redact(value, secrets=()):
    """Bounded text scrubbing; no provider content is included in errors."""
    if not isinstance(value, str):
        return ''
    for secret in secrets:
        if isinstance(secret, str) and secret:
            value = value.replace(secret, '[已隐藏密钥]')
    value = _TOKEN.sub('[已隐藏密钥]', value)
    value = _BEARER.sub('Bearer [已隐藏密钥]', value)
    value = _SECRET_ASSIGNMENT.sub(lambda match: match[1] + '[已隐藏密钥]', value)
    return ''.join(char for char in value if char in '\n\t' or ord(char) >= 32)


def validate(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError('独立审核 API 设置格式不正确')
    clean = {key: value.get(key, default) for key, default in DEFAULT.items()}
    for key in ('enabled', 'json_mode', 'disable_thinking', 'consent'):
        if type(clean[key]) is not bool:
            raise ValueError('独立审核 API 开关格式不正确')
    for key, low, high in (('timeout_seconds', 5, 120), ('output_tokens', 32, 4096), ('context_messages', 1, 40)):
        if type(clean[key]) is not int or not low <= clean[key] <= high:
            raise ValueError('独立审核 API 时长或输出上限超出范围')
    if not isinstance(clean['base_url'], str) or not isinstance(clean['model'], str):
        raise ValueError('独立审核 API 地址或模型名称格式不正确')
    url = clean['base_url'].strip().rstrip('/')
    model = clean['model'].strip()
    if len(url) > 2048 or any(ord(c) < 33 for c in url) or '\\' in url:
        raise ValueError('独立审核 API 地址格式不正确')
    if url:
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            parsed.port  # Reject malformed or out-of-range ports.
            loopback = host == 'localhost' or bool(host and ipaddress.ip_address(host).is_loopback)
        except ValueError:
            # An ordinary DNS name is valid HTTPS; malformed URL/ports are not.
            try:
                parsed = urlsplit(url)
                host = parsed.hostname
                parsed.port
                loopback = host == 'localhost'
            except ValueError:
                raise ValueError('独立审核 API 地址格式不正确') from None
        if not host or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError('独立审核 API 地址不能包含密钥、查询参数或片段')
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and loopback):
            raise ValueError('独立审核 API 须使用 HTTPS；本机服务可使用 HTTP')
        if '?' in url or '#' in url or '%' in parsed.netloc:
            raise ValueError('独立审核 API 地址格式不正确')
    if len(model) > 150 or any(ord(c) < 32 for c in model):
        raise ValueError('独立审核模型名称格式不正确')
    clean.update(base_url=url, model=model)
    if clean['enabled'] and (not url or not model or not clean['consent']):
        raise ValueError('启用独立审核前须配置地址、模型并明确同意发送群消息')
    return clean


def _path(path):
    return Path(path) if path is not None else ROOT / 'moderation-api.json'


def _key(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError('独立审核 API 密钥格式不正确')
    return value


def load(path=None):
    """Private runtime configuration. Never serialize this return value publicly."""
    try:
        value = json.loads(_path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        value = {}
    except (OSError, UnicodeError, ValueError):
        raise ValueError('独立审核 API 配置无法读取') from None
    clean = validate(value)
    clean['api_key'] = _key(value.get('api_key', ''))
    return clean


def public(config=None, path=None):
    config = load(path) if config is None else config
    clean = validate(config)
    # has_key is preserved when sanitizing a public settings object; a private
    # config's actual secret always takes precedence over caller metadata.
    clean['has_key'] = bool(config.get('api_key')) if 'api_key' in config else config.get('has_key') is True
    return clean


def save(value, key='', path=None):
    if not isinstance(value, dict):
        raise ValueError('独立审核 API 设置格式不正确')
    if type(value.get('clear_key', False)) is not bool:
        raise ValueError('独立审核 API 密钥清除开关格式不正确')
    key = _key(key)
    previous = load(path)
    merged = {**previous, **{k: value[k] for k in DEFAULT if k in value}}
    # A consent checked for an old endpoint never transfers to a new recipient.
    comparable = validate({**merged, 'enabled': False})
    changed = any(comparable[k] != previous[k] for k in ('base_url', 'model'))
    if changed:
        merged['consent'] = False
    clean = validate(merged)
    endpoint_changed = comparable['base_url'] != previous['base_url']
    clean['api_key'] = '' if value.get('clear_key') else key or ('' if endpoint_changed else previous['api_key'])
    if clean['enabled'] and not clean['api_key']:
        raise ValueError('启用独立审核前须填写独立 API 密钥')
    destination = _path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='moderation-api-', suffix='.tmp', dir=str(destination.parent))
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(clean, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except OSError:
        raise ValueError('独立审核 API 配置无法保存') from None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return public(clean)


def request_descriptor(messages, config=None, path=None, test=False):
    """Private OpenAI-compatible request; never falls back to chat settings."""
    if type(test) is not bool:
        raise ValueError('独立审核测试开关格式不正确')
    config = load(path) if config is None else config
    clean = validate(config)
    if not clean['enabled'] and not test:
        raise ValueError('独立审核 API 尚未启用')
    if not clean['base_url'] or not clean['model'] or not clean['consent']:
        raise ValueError('发送审核请求前须明确同意向当前地址发送群消息')
    key = _key(config.get('api_key', ''))
    if not key:
        raise ValueError('独立审核 API 尚未配置密钥')
    if not isinstance(messages, list) or not 1 <= len(messages) <= 8:
        raise ValueError('独立审核请求格式不正确')
    safe_messages = []
    for message in messages:
        if not isinstance(message, dict) or message.get('role') not in ('system', 'user', 'assistant') or not isinstance(message.get('content'), str):
            raise ValueError('独立审核请求格式不正确')
        safe_messages.append({'role': message['role'], 'content': redact(message['content'], (key,))})
    if sum(len(m['content']) for m in safe_messages) > 160000:
        raise ValueError('独立审核请求内容过长')
    payload = {'model': clean['model'], 'messages': safe_messages,
               'max_tokens': clean['output_tokens'], 'temperature': 0}
    if clean['json_mode']:
        payload['response_format'] = {'type': 'json_object'}
    if clean['disable_thinking']:
        payload['thinking'] = {'type': 'disabled'}
    return {'url': clean['base_url'] + '/chat/completions', 'payload': payload,
            'api_key': key, 'timeout_seconds': clean['timeout_seconds'],
            'service_override': {'base_url': clean['base_url'], 'api_key': key},
            'policy_override': {'output_tokens': clean['output_tokens'],
                                'disable_thinking': clean['disable_thinking'],
                                'request_timeout': clean['timeout_seconds']}}


def _sender(row):
    for key in ('user_id', '_user_id', 'sender_ref', 'member_ref'):
        if row.get(key) is not None and str(row[key]):
            return str(row[key])
    return None


def _sources(row):
    sources = row.get('sources', row.get('segments'))
    return sources if isinstance(sources, list) else [row]


def _source_id(row):
    for key in ('source_id', 'id', '_source_id', 'message_id', '_message_id'):
        if row.get(key) is not None:
            return str(row[key])
    return None


def validate_verdicts(raw, window, targets):
    """Parse data, never commands. Only exact target-author evidence is eligible.

    window rows require immutable source id/text and actual sender information;
    collected rows may contain sources/segments. targets are window indices or
    dictionaries with index plus current user_id. Display names are never used.
    Missing, background, ambiguous or mismatched evidence returns a review-only
    verdict; an invalid/refused provider schema raises a fixed safe ValueError.
    """
    invalid = '审核模型结果无法验证，请人工复核'
    if isinstance(raw, str):
        if len(raw) > 131072:
            raise ValueError(invalid)
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raise ValueError(invalid) from None
    if not isinstance(raw, dict) or set(raw) != {'violations', 'reviewed_indices'} or not isinstance(raw['violations'], list) or len(raw['violations']) > 20:
        raise ValueError(invalid)
    if not isinstance(window, list) or len(window) > 60 or any(not isinstance(r, dict) for r in window) or not isinstance(targets, (list, tuple, set)):
        raise ValueError(invalid)
    target_senders = {}
    for target in targets:
        index = target.get('index') if isinstance(target, dict) else target
        if type(index) is not int or not 0 <= index < len(window):
            raise ValueError(invalid)
        target_senders[index] = _sender(target) if isinstance(target, dict) else _sender(window[index])
    reviewed = raw['reviewed_indices']
    if (not isinstance(reviewed, list) or any(type(i) is not int for i in reviewed)
            or len(reviewed) != len(set(reviewed)) or set(reviewed) != set(target_senders)):
        raise ValueError(invalid)
    source_map = {}
    for index, row in enumerate(window):
        for source in _sources(row):
            if not isinstance(source, dict):
                raise ValueError(invalid)
            ident = _source_id(source)
            if ident is not None:
                source_map.setdefault(ident, []).append((index, source, _sender(source) or _sender(row)))
    result, seen = [], set()
    required = {'index', 'category', 'confidence', 'direct_violation', 'evidence', 'reason'}
    for verdict in raw['violations']:
        if not isinstance(verdict, dict) or set(verdict) != required:
            raise ValueError(invalid)
        index, category, confidence = verdict['index'], verdict['category'], verdict['confidence']
        if (type(index) is not int or not 0 <= index < len(window) or index in seen
                or not isinstance(category, str) or category not in CATEGORIES
                or type(confidence) not in (int, float) or not 0 <= confidence <= 1 or not math.isfinite(confidence)
                or type(verdict['direct_violation']) is not bool
                or not isinstance(verdict['reason'], str) or len(verdict['reason']) > 1000
                or not isinstance(verdict['evidence'], list) or len(verdict['evidence']) > 12):
            raise ValueError(invalid)
        seen.add(index)
        sender = target_senders.get(index)
        eligible = index in target_senders and sender is not None and _sender(window[index]) == sender and bool(verdict['evidence'])
        evidence = []
        for entry in verdict['evidence']:
            if not isinstance(entry, dict) or set(entry) != {'source_id', 'quote'} or not isinstance(entry['source_id'], (str, int)) or type(entry['source_id']) is bool or not isinstance(entry['quote'], str) or len(entry['quote']) > 2000:
                raise ValueError(invalid)
            ident, quote = str(entry['source_id']), entry['quote']
            records = source_map.get(ident, [])
            matches = [(i, s, u) for i, s, u in records if i == index]
            # Duplicate source ids anywhere in the window are ambiguous.
            if len(records) != 1 or len(matches) != 1 or not quote.strip():
                eligible = False
            else:
                _, source, author = matches[0]
                if author != sender or not isinstance(source.get('text'), str) or quote not in source['text']:
                    eligible = False
            evidence.append({'source_id': ident[:200], 'quote': redact(quote)[:2000]})
        clean = {'index': index, 'category': category, 'confidence': confidence,
                 'direct_violation': verdict['direct_violation'], 'evidence': evidence,
                 'reason': redact(verdict['reason'])[:1000],
                 'autoeligible': bool(eligible and verdict['direct_violation'])}
        result.append(clean)
    return result
