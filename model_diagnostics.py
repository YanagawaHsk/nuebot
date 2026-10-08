"""Bounded provider diagnostics containing no request or provider message text."""

import json
import math
import re
import threading
import time
import urllib.error
from email.utils import parsedate_to_datetime

_BODY_LIMIT = 8192
_CACHE_ATTRIBUTE = '_model_diagnostic_fields'
_CACHE_LOCK = threading.RLock()
_PROVIDER_CLASSES = frozenset(('rate_requests', 'rate_tokens', 'quota', 'capacity', 'unknown'))
_REQUEST_ID_HEADERS = ('x-request-id', 'request-id', 'openai-request-id',
                       'x-amzn-requestid', 'x-ms-request-id')
_REQUEST_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z', re.ASCII)
_NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z', re.ASCII)
_CODE_CLASSES = {
    'rate_limit_requests': 'rate_requests',
    'requests_per_minute_exceeded': 'rate_requests',
    'rpm_limit_exceeded': 'rate_requests',
    'rate_limit_tokens': 'rate_tokens',
    'tokens_per_minute_exceeded': 'rate_tokens',
    'tpm_limit_exceeded': 'rate_tokens',
    'insufficient_quota': 'quota',
    'billing_hard_limit_reached': 'quota',
    'quota_exceeded': 'quota',
    'insufficient_balance': 'quota',
    'credit_balance_too_low': 'quota',
    'overloaded_error': 'capacity',
    'model_overloaded': 'capacity',
    'server_overloaded': 'capacity',
    'capacity_exceeded': 'capacity',
}
_TYPE_CLASSES = {
    'requests': 'rate_requests',
    'request_rate_limit': 'rate_requests',
    'tokens': 'rate_tokens',
    'token_rate_limit': 'rate_tokens',
    'insufficient_quota': 'quota',
    'billing_hard_limit_reached': 'quota',
    'quota_exceeded': 'quota',
    'insufficient_balance': 'quota',
    'overloaded_error': 'capacity',
    'capacity_error': 'capacity',
}
# These are fixed provider phrases, never generic mentions of quota/rate.
# Message text is inspected only locally and is never attached or returned.
_QUOTA_PHRASES = (
    'you exceeded your current quota, please check your plan and billing details',
    'your credit balance is too low to access the anthropic api',
)
_STRONG_QUOTA_NAMES = frozenset(('billing_hard_limit_reached', 'insufficient_balance', 'credit_balance_too_low'))
_TOKEN_RATE_PHRASES = ('inference tpm exhausted', 'tokens per minute exceeded')
_REQUEST_RATE_PHRASES = ('requests per minute exceeded', 'rpm limit exceeded')


def _header(headers, name):
    if headers is None:
        return None
    try:
        for spelling in (name, name.title(), name.upper()):
            value = headers.get(spelling)
            if value is not None:
                return value
        # Plain dicts are case sensitive even though HTTP header names are not.
        for index, (key, value) in enumerate(headers.items()):
            if index >= 256:
                break
            if type(key) is str and key.lower() == name:
                return value
    except Exception:
        pass
    return None


def _safe_request_id(value):
    if type(value) is not str or not _REQUEST_ID.fullmatch(value):
        return None
    if value.lower().startswith(('sk-', 'sk_')):
        return None
    return value


def _request_id(headers):
    for name in _REQUEST_ID_HEADERS:
        value = _safe_request_id(_header(headers, name))
        if value is not None:
            return value
    return None


def _finite_now(now):
    if type(now) in (int, float):
        try:
            if math.isfinite(now):
                return float(now)
        except (OverflowError, ValueError):
            pass
    return time.time()


def retry_after(headers, now=None):
    """Return a finite Retry-After delay clamped to 0..3600, or None."""
    try:
        value = _header(headers, 'retry-after')
        if type(value) in (int, float):
            delay = float(value)
        elif type(value) is str and len(value) <= 128:
            value = value.strip()
            if _NUMBER.fullmatch(value):
                delay = float(value)
            else:
                date = parsedate_to_datetime(value)
                # HTTP dates include a timezone. Reject ambiguous local dates.
                if date.tzinfo is None:
                    return None
                delay = date.timestamp() - _finite_now(now)
        else:
            return None
        if not math.isfinite(delay):
            return None
        return min(3600.0, max(0.0, delay))
    except Exception:
        return None


def _structured_name(value):
    return value.strip().lower() if type(value) is str and len(value) <= 80 else None


def _classify(payload):
    if type(payload) is not dict or type(payload.get('error')) is not dict:
        return 'unknown'
    error = payload['error']
    code, kind = _structured_name(error.get('code')), _structured_name(error.get('type'))
    # Specific balance/billing evidence cannot be overridden by token words
    # contained in an otherwise unrelated provider message.
    if code in _STRONG_QUOTA_NAMES or kind in _STRONG_QUOTA_NAMES:
        return 'quota'
    code_class = _CODE_CLASSES.get(code)
    result = code_class or _TYPE_CLASSES.get(kind) or 'unknown'
    message = error.get('message')
    lowered = message.lower() if type(message) is str else ''
    # Some providers use insufficient_quota for an allocated TPM limit.
    # Only these fixed rate phrases refine that broad code/type. A generic
    # quota/rate hint or an HTTP status never supplies the missing evidence.
    if result == 'unknown' or code == 'insufficient_quota' or (code_class is None and kind == 'insufficient_quota'):
        token_rate = any(phrase in lowered for phrase in _TOKEN_RATE_PHRASES)
        request_rate = any(phrase in lowered for phrase in _REQUEST_RATE_PHRASES)
        if token_rate and request_rate:
            return 'unknown'
        if token_rate:
            return 'rate_tokens'
        if request_rate:
            return 'rate_requests'
    if result != 'unknown':
        return result
    if lowered:
        if any(phrase in lowered for phrase in _QUOTA_PHRASES):
            return 'quota'
    return 'unknown'


def _safe_error_fields(fields):
    """Copy and validate the cache so callers cannot mutate later diagnostics."""
    out = {'provider_class': 'unknown'}
    if type(fields) is not dict:
        return out
    provider_class = fields.get('provider_class')
    if type(provider_class) is str and provider_class in _PROVIDER_CLASSES:
        out['provider_class'] = provider_class
    status = fields.get('http_status')
    if type(status) is int and 100 <= status <= 599:
        out['http_status'] = status
    request_id = _safe_request_id(fields.get('request_id'))
    if request_id is not None:
        out['request_id'] = request_id
    delay = fields.get('retry_after_seconds')
    if type(delay) in (int, float):
        try:
            if math.isfinite(delay) and 0 <= delay <= 3600:
                out['retry_after_seconds'] = float(delay)
        except (OverflowError, ValueError):
            pass
    return out


def error_fields(exc, now=None):
    """Read an HTTP error body once and cache only allowlisted safe fields.

    Generic HTTP 429 and 5xx statuses do not imply a quota or capacity class.
    The single read is capped at 8193 bytes; over-limit bodies are not parsed.
    """
    with _CACHE_LOCK:
        try:
            cached = getattr(exc, _CACHE_ATTRIBUTE, None)
            if type(cached) is dict:
                return _safe_error_fields(cached)
        except Exception:
            pass
        out = {'provider_class': 'unknown'}
        try:
            if isinstance(exc, urllib.error.HTTPError):
                if type(exc.code) is int and 100 <= exc.code <= 599:
                    out['http_status'] = exc.code
                headers = exc.headers
                request_id = _request_id(headers)
                if request_id is not None:
                    out['request_id'] = request_id
                delay = retry_after(headers, now)
                if delay is not None:
                    out['retry_after_seconds'] = delay
                try:
                    body = exc.read(_BODY_LIMIT + 1)
                    if type(body) in (bytes, bytearray) and len(body) <= _BODY_LIMIT:
                        out['provider_class'] = _classify(json.loads(body))
                except Exception:
                    pass
        except Exception:
            pass
        safe = _safe_error_fields(out)
        try:
            setattr(exc, _CACHE_ATTRIBUTE, safe.copy())
        except Exception:
            pass
        return safe


def response_fields(response, headers=None):
    """Extract only bounded integer usage and a safe request-id header."""
    out = {}
    try:
        if type(response) is dict and type(response.get('usage')) is dict:
            usage = response['usage']
            for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                value = usage.get(key)
                if type(value) is int and 0 <= value <= 1_000_000_000:
                    out['usage_' + key] = value
        request_id = _request_id(headers)
        if request_id is not None:
            out['request_id'] = request_id
    except Exception:
        pass
    return out
