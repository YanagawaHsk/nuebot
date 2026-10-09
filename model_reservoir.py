"""Local, provider-wide input/output token buckets, not provider account quota.

Only estimates, safe usage counts and opaque reservation IDs are persisted.
Reservations are immediate: callers release the model lease and requeue when
refill is needed. No request body, prompt, key or provider message is stored.
"""
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import model_gate

ROOT = Path(__file__).resolve().parent
_RESERVATION_GRACE = 30
_USAGE_MAX = 1000000000
_REJECTED = {400, 401, 402, 403, 404, 405, 406, 409, 410, 411, 412,
             413, 414, 415, 416, 417, 422, 423, 424, 426, 428, 429, 431, 451}


class ReservoirTooSmall(ValueError):
    def __init__(self, code):
        self.code = code if code in ('InputReservoirTooSmall', 'OutputReservoirTooSmall') else 'InputReservoirTooSmall'
        super().__init__(self.code)


def estimate_input(body):
    """UTF-8 byte count is a conservative local estimate without a tokenizer."""
    # Match the actual Request serializer, including its JSON whitespace.
    return len(json.dumps(body, ensure_ascii=False).encode('utf-8'))


@contextmanager
def db():
    conn = sqlite3.connect(ROOT / 'model-reservoir.sqlite', timeout=3)
    try:
        with conn:
            conn.execute('CREATE TABLE IF NOT EXISTS buckets(service TEXT PRIMARY KEY,input_tokens REAL NOT NULL,output_tokens REAL NOT NULL,updated REAL NOT NULL)')
            columns = {row[1] for row in conn.execute('PRAGMA table_info(buckets)')}
            for name in ('input_capacity', 'output_capacity', 'input_rate', 'output_rate'):
                if name not in columns:
                    try: conn.execute('ALTER TABLE buckets ADD COLUMN ' + name + ' REAL NOT NULL DEFAULT 0')
                    except sqlite3.OperationalError as exc:
                        if 'duplicate column' not in str(exc).lower(): raise
            conn.execute('CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY,service TEXT NOT NULL,input_tokens INTEGER NOT NULL,output_tokens INTEGER NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,updated REAL NOT NULL)')
            conn.execute('CREATE INDEX IF NOT EXISTS reservations_service ON reservations(service,state,expires)')
            yield conn
    finally:
        conn.close()


def _number(value):
    return value if type(value) is int and 0 <= value <= _USAGE_MAX else None


def _configure(conn, key, policy, now):
    values = tuple(policy.get(name, model_gate.DEFAULT[name]) for name in
                   ('input_capacity_tokens', 'output_capacity_tokens',
                    'input_refill_tokens_per_minute', 'output_refill_tokens_per_minute'))
    if any(type(value) is not int or not 1 <= value <= 10000000 for value in values):
        raise ValueError('模型蓄水池参数超出范围')
    conn.execute('INSERT OR IGNORE INTO buckets VALUES(?,?,?,?,?,?,?,?)',
                 (key, values[0], values[1], now, *values))
    _refill(conn, key, now, values)
    return values


def _refill(conn, key, now, values=None):
    row = conn.execute('SELECT input_tokens,output_tokens,updated,input_capacity,output_capacity,input_rate,output_rate FROM buckets WHERE service=?', (key,)).fetchone()
    if row is None: return
    caps = values[:2] if values else row[3:5]
    rates = values[2:] if values else row[5:7]
    elapsed = max(0, now - row[2])
    old_rates = tuple(row[5 + i] or rates[i] for i in range(2))
    balances = tuple(min(caps[i], row[i] + elapsed * old_rates[i] / 60) for i in range(2))
    conn.execute('UPDATE buckets SET input_tokens=?,output_tokens=?,updated=?,input_capacity=?,output_capacity=?,input_rate=?,output_rate=? WHERE service=?',
                 (*balances, now, *caps, *rates, key))


def _credit(conn, key, input_delta, output_delta):
    conn.execute('UPDATE buckets SET input_tokens=MIN(input_capacity,input_tokens+?),output_tokens=MIN(output_capacity,output_tokens+?) WHERE service=?',
                 (input_delta, output_delta, key))


def _recover(conn, key, now):
    for ident, inputs, outputs in conn.execute('SELECT id,input_tokens,output_tokens FROM reservations WHERE service=? AND state="reserved" AND expires<=?', (key, now)).fetchall():
        _credit(conn, key, inputs, outputs)
        conn.execute('UPDATE reservations SET state="expired",updated=? WHERE id=? AND state="reserved"', (now, ident))
    # An interrupted real HTTP call may already have consumed both budgets.
    conn.execute('UPDATE reservations SET state="abandoned",updated=? WHERE service=? AND state="started" AND expires<=?', (now, key, now))
    conn.execute('DELETE FROM reservations WHERE service=? AND state NOT IN ("reserved","started") AND updated<?', (key, now - 86400))


def reserve(key, input_tokens, output_tokens, policy, cancel=lambda: False,
            deadline=None, notice=None, purpose=None):
    """Atomically debit both buckets or return one bounded wait exception.

    Call inside model_gate.acquire(), before start_request(). No sleeping or
    partial debit occurs on insufficient tokens. A request larger than either
    bucket capacity is a configuration error, not a retryable wait.
    """
    if cancel(): raise model_gate.Cancelled('ReplySuperseded')
    if _number(input_tokens) is None or _number(output_tokens) is None:
        raise ValueError('模型token预估格式不正确')
    now = time.time()
    if deadline is not None and now >= deadline:
        raise model_gate.QueueExpired('ReplyExpired', reason='deadline', now=now)
    if not policy.get('reservoir_enabled', True): return None
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        now = time.time()
        if cancel(): raise model_gate.Cancelled('ReplySuperseded')
        if deadline is not None and now >= deadline:
            raise model_gate.QueueExpired('ReplyExpired', reason='deadline', now=now)
        capacities = _configure(conn, key, policy, now)
        if input_tokens > capacities[0]: raise ReservoirTooSmall('InputReservoirTooSmall')
        if output_tokens > capacities[1]: raise ReservoirTooSmall('OutputReservoirTooSmall')
        _recover(conn, key, now)
        available = conn.execute('SELECT input_tokens,output_tokens FROM buckets WHERE service=?', (key,)).fetchone()
        waits = [max(0, requested - balance) * 60 / rate for requested, balance, rate in
                 zip((input_tokens, output_tokens), available, capacities[2:])]
        if max(waits) > 0:
            reason = 'input_bucket' if waits[0] >= waits[1] else 'output_bucket'
            if notice: notice('reservoir_wait')
            raise model_gate.QueueExpired('ModelReservoirTimeout', reason=reason,
                                          next_ready_at=now + max(waits), now=now)
        ident = uuid.uuid4().hex
        expires = deadline if deadline is not None else now + policy.get('queue_timeout', 25) + policy.get('request_timeout', 30) + _RESERVATION_GRACE
        _credit(conn, key, -input_tokens, -output_tokens)
        conn.execute('INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?)',
                     (ident, key, input_tokens, output_tokens, 'reserved', now, expires, now))
    # Cancellation after lock release must also refund the precise reservation.
    if cancel():
        refund(ident)
        raise model_gate.Cancelled('ReplySuperseded')
    return ident


def mark_started(ident):
    if ident is None: return True
    expired = False
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE'); now = time.time()
        row = conn.execute('SELECT service,input_tokens,output_tokens,state,expires FROM reservations WHERE id=?', (ident,)).fetchone()
        if row is None or row[3] not in ('reserved', 'started'):
            expired = True
        elif row[3] == 'reserved' and row[4] <= now:
            _refill(conn, row[0], now); _credit(conn, row[0], row[1], row[2])
            conn.execute('UPDATE reservations SET state="expired",updated=? WHERE id=?', (now, ident)); expired = True
        elif row[3] == 'reserved':
            conn.execute('UPDATE reservations SET state="started",expires=MAX(expires,?),updated=? WHERE id=?', (now + 120, now, ident))
    if expired: raise model_gate.QueueExpired('ReplyExpired', reason='deadline')
    return True


def refund(ident):
    """Refund only a reservation that never opened HTTP; safe to repeat."""
    if ident is None: return False
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE'); now = time.time()
        row = conn.execute('SELECT service,input_tokens,output_tokens,state FROM reservations WHERE id=?', (ident,)).fetchone()
        if row is None or row[3] != 'reserved': return False
        _refill(conn, row[0], now); _credit(conn, row[0], row[1], row[2])
        conn.execute('UPDATE reservations SET state="refunded",updated=? WHERE id=?', (now, ident))
    return True


def abort_before_http(ident):
    """Caller-confirmed pre-open abort, including the mark_started/commit gap.

    This stronger refund is for the caller that has verified opener.open was
    never invoked. Ordinary refund() cannot release a started reservation.
    """
    if ident is None: return False
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE'); now = time.time()
        row = conn.execute('SELECT service,input_tokens,output_tokens,state FROM reservations WHERE id=?', (ident,)).fetchone()
        if row is None or row[3] not in ('reserved', 'started'): return False
        _refill(conn, row[0], now); _credit(conn, row[0], row[1], row[2])
        conn.execute('UPDATE reservations SET state="aborted",updated=? WHERE id=?', (now, ident))
    return True


def settle(ident, usage=None):
    """Correct valid usage per bucket; missing/invalid usage keeps the estimate."""
    if ident is None: return False
    usage = usage if isinstance(usage, dict) else {}
    if isinstance(usage.get('usage'), dict): usage = usage['usage']
    actual_in = _number(usage.get('usage_prompt_tokens', usage.get('prompt_tokens')))
    actual_out = _number(usage.get('usage_completion_tokens', usage.get('completion_tokens')))
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE'); now = time.time()
        row = conn.execute('SELECT service,input_tokens,output_tokens,state FROM reservations WHERE id=?', (ident,)).fetchone()
        if row is None or row[3] not in ('started', 'abandoned'): return False
        _refill(conn, row[0], now)
        _credit(conn, row[0], row[1] - actual_in if actual_in is not None else 0,
                row[2] - actual_out if actual_out is not None else 0)
        conn.execute('UPDATE reservations SET state="settled",updated=? WHERE id=?', (now, ident))
    return True


def fail(ident, http_status=None):
    """Keep input after real HTTP; refund output only for definite rejection."""
    if ident is None: return False
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE'); now = time.time()
        row = conn.execute('SELECT service,input_tokens,output_tokens,state FROM reservations WHERE id=?', (ident,)).fetchone()
        if row is None or row[3] not in ('reserved', 'started', 'abandoned'): return False
        _refill(conn, row[0], now)
        _credit(conn, row[0], row[1] if row[3] == 'reserved' else 0,
                row[2] if row[3] == 'reserved' or type(http_status) is int and http_status in _REJECTED else 0)
        conn.execute('UPDATE reservations SET state="failed",updated=? WHERE id=?', (now, ident))
    return True


def snapshot(key, policy):
    enabled = policy.get('reservoir_enabled', True)
    values = {name: policy.get(name, model_gate.DEFAULT[name]) for name in
              ('input_capacity_tokens', 'output_capacity_tokens', 'input_refill_tokens_per_minute', 'output_refill_tokens_per_minute')}
    if not enabled: return {'enabled': False, **values, 'reserved': 0, 'active': 0}
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE'); now = time.time()
        _configure(conn, key, policy, now); _recover(conn, key, now)
        balances = conn.execute('SELECT input_tokens,output_tokens FROM buckets WHERE service=?', (key,)).fetchone()
        counts = dict(conn.execute('SELECT state,COUNT(*) FROM reservations WHERE service=? AND state IN ("reserved","started") GROUP BY state', (key,)))
    return {'enabled': True, **values,
            'input_available_tokens': max(0, math.floor(balances[0])),
            'output_available_tokens': max(0, math.floor(balances[1])),
            'input_debt_tokens': max(0, math.ceil(-balances[0])),
            'output_debt_tokens': max(0, math.ceil(-balances[1])),
            'reserved': counts.get('reserved', 0), 'active': counts.get('started', 0),
            'estimate_method': 'utf8_bytes'}
