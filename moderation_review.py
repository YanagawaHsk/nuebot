"""Bounded group-local review records and an at-most-once action bridge.

No network, warnings or mute actions occur here. The authenticated panel may
close/correct cases locally or request a warning; only the bot owner loop claims
and executes requests. A crashed claim is unknown and is never replayed.
"""
import hashlib
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from moderation_api import redact

ROOT = Path(__file__).resolve().parent
STATES = frozenset(('pending', 'processing', 'resolved', 'unknown'))
MAX_CASES = 500
RETENTION_DAYS = 30
WARNING_TTL = 120
CLAIM_LEASE_SECONDS = 300


def _clock(now):
    value = time.time() if now is None else now
    if type(value) not in (int, float) or not 0 <= value <= 32503680000 or not math.isfinite(value):
        raise ValueError('复核时间格式不正确')
    return value


def _fingerprint(sources):
    """Exact content only; this never exempts an identity or executes a rule."""
    texts = [row.get('text', '') for row in sources]
    return hashlib.sha256(json.dumps(texts, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _event_keys(sources):
    return [hashlib.sha256(json.dumps([str(s.get('id')), str(s.get('user_id'))], separators=(',', ':')).encode()).hexdigest() for s in sources]


class Store:
    def __init__(self, group_id, root=None, *, max_cases=MAX_CASES,
                 retention_days=RETENTION_DAYS, warning_ttl=WARNING_TTL,
                 secrets=()):
        group = str(group_id)
        if not group.isascii() or not group.isdigit() or not 1 <= len(group) <= 12:
            raise ValueError('复核群号格式不正确')
        for value, low, high in ((max_cases, 1, 500), (retention_days, 1, 30), (warning_ttl, 1, 120)):
            if type(value) is not int or not low <= value <= high:
                raise ValueError('复核保留设置超出范围')
        self.group_id = group
        self.path = (Path(root) if root is not None else ROOT) / 'moderation-review' / (group + '.sqlite')
        self.max_cases, self.retention_days, self.warning_ttl = max_cases, retention_days, warning_ttl
        self.secrets = tuple(secret for secret in secrets if isinstance(secret, str) and secret)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db():
            pass

    @contextmanager
    def _db(self):
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA secure_delete=ON')
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('''CREATE TABLE IF NOT EXISTS cases(
                id TEXT PRIMARY KEY, case_key TEXT UNIQUE NOT NULL, group_id TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, received_at REAL NOT NULL,
                state TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL,
                requested_action TEXT NOT NULL DEFAULT '', requested_at REAL,
                request_reason TEXT NOT NULL DEFAULT '', claim_token TEXT NOT NULL DEFAULT '',
                claim_at REAL, result TEXT NOT NULL DEFAULT '')''')
            conn.execute('CREATE INDEX IF NOT EXISTS review_state_created ON cases(state,created_at)')
            conn.execute('''CREATE TABLE IF NOT EXISTS action_ledger(
                event_key TEXT PRIMARY KEY, case_id TEXT NOT NULL, action TEXT NOT NULL,
                claimed_at REAL NOT NULL, state TEXT NOT NULL)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS corrections(
                fingerprint TEXT PRIMARY KEY, case_id TEXT NOT NULL, reason TEXT NOT NULL,
                created_at REAL NOT NULL)''')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _text(self, value, limit):
        return redact(value, self.secrets)[:limit] if isinstance(value, str) else ''

    def _safe(self, value, depth=0):
        if depth > 6:
            return None
        if isinstance(value, str):
            return self._text(value, 2000)
        if type(value) is int:
            return value if -(2 ** 63) <= value < 2 ** 63 else None
        if type(value) is bool or value is None:
            return value
        if type(value) is float:
            return value if math.isfinite(value) else None
        if isinstance(value, list):
            return [self._safe(v, depth + 1) for v in value[:40]]
        if isinstance(value, dict):
            return {self._text(k, 100): self._safe(v, depth + 1) for k, v in list(value.items())[:30]
                    if isinstance(k, str) and not any(term in k.lower() for term in ('api_key', 'access_token', 'authorization', 'password', 'secret'))}
        return None

    def _sources(self, value, now, *, context=False):
        if not isinstance(value, list) or (not context and not 1 <= len(value) <= 40):
            raise ValueError('复核原始消息格式不正确')
        result, total = [], 0
        for row in value[:40]:
            if not isinstance(row, dict) or not isinstance(row.get('text'), str):
                raise ValueError('复核原始消息格式不正确')
            ident = row.get('id', row.get('source_id', row.get('message_id')))
            user = row.get('user_id', row.get('_user_id', row.get('sender_ref')))
            if not context and (ident is None or user is None or type(ident) not in (str, int) or type(user) not in (str, int)):
                raise ValueError('复核原始消息身份格式不正确')
            received = row.get('received_at', now)
            if type(received) not in (int, float) or not 0 <= received <= 32503680000 or not math.isfinite(received):
                received = now
            text = self._text(row['text'], min(8000, max(0, 160000 - total)))
            total += len(text)
            clean = {'id': self._text(str(ident or ''), 200), 'text': text,
                     'user_id': self._text(str(user or ''), 100), 'received_at': min(received, now)}
            for key in ('message_id', 'sender_ref', 'speaker'):
                if row.get(key) is not None:
                    clean[key] = self._text(str(row[key]), 200)
            result.append(clean)
        return result

    def _maintain(self, conn, now):
        cutoff = now - self.retention_days * 86400
        stale = conn.execute("SELECT id FROM cases WHERE state='processing' AND claim_at<=?", (now - CLAIM_LEASE_SECONDS,)).fetchall()
        for row in stale:
            conn.execute("UPDATE cases SET state='unknown',revision=revision+1,updated_at=?,result='处理被中断，结果未知；不会自动重试',requested_action='',claim_token='' WHERE id=?", (now, row['id']))
            conn.execute("UPDATE action_ledger SET state='unknown' WHERE case_id=?", (row['id'],))
        conn.execute('DELETE FROM cases WHERE created_at<?', (cutoff,))
        conn.execute('DELETE FROM cases WHERE id IN (SELECT id FROM cases ORDER BY created_at DESC,id DESC LIMIT -1 OFFSET ?)', (self.max_cases,))
        conn.execute('DELETE FROM corrections WHERE created_at<?', (cutoff,))
        conn.execute('DELETE FROM corrections WHERE fingerprint IN (SELECT fingerprint FROM corrections ORDER BY created_at DESC LIMIT -1 OFFSET ?)', (self.max_cases,))
        conn.execute('DELETE FROM action_ledger WHERE claimed_at<?', (cutoff,))
        conn.execute('DELETE FROM action_ledger WHERE event_key IN (SELECT event_key FROM action_ledger ORDER BY claimed_at DESC LIMIT -1 OFFSET 20000)')

    def _row(self, row, now, private=False):
        if row is None:
            return None
        value = dict(row)
        data = json.loads(value.pop('data'))
        value.pop('case_key', None)
        if not private:
            value.pop('claim_token', None)
        value.update(data)
        value['warning_expired'] = now - value['received_at'] >= self.warning_ttl
        value['can_warn'] = value['state'] == 'pending' and not value['requested_action'] and not value['warning_expired']
        return value

    def _find(self, conn, ident):
        if not isinstance(ident, str) or len(ident) > 100:
            raise ValueError('复核记录编号格式不正确')
        return conn.execute('SELECT * FROM cases WHERE id=? AND group_id=?', (ident, self.group_id)).fetchone()

    @staticmethod
    def _revision(row, expected_revision):
        if row is None:
            raise ValueError('此群没有该复核记录')
        if type(expected_revision) is not int or expected_revision != row['revision']:
            raise ValueError('复核记录已更新，请刷新后重试')

    def append(self, case=None, *, sources=None, context=None, verdict=None,
               reason='', code='', now=None):
        now = _clock(now)
        if case is not None:
            if not isinstance(case, dict):
                raise ValueError('复核记录格式不正确')
            if case.get('group_id') is not None and str(case['group_id']) != self.group_id:
                raise ValueError('不能写入其他群的复核记录')
            sources, context, verdict = case.get('sources', sources), case.get('context', context), case.get('verdict', verdict)
            reason, code = case.get('reason', reason), case.get('code', code)
        original = sources
        sources = self._sources(sources, now)
        context = self._sources(context or [], now, context=True)
        fingerprint = _fingerprint(original)
        event_keys = _event_keys(sources)
        case_key = hashlib.sha256(json.dumps([event_keys, fingerprint], separators=(',', ':')).encode()).hexdigest()
        data = {'sources': sources, 'context': context, 'verdict': self._safe(verdict),
                'reason': self._text(reason, 1000), 'code': self._text(code, 100),
                'content_fingerprint': fingerprint, 'event_keys': event_keys}
        received = min(s['received_at'] for s in sources)
        with self._db() as conn:
            self._maintain(conn, now)
            existing = conn.execute('SELECT * FROM cases WHERE case_key=?', (case_key,)).fetchone()
            if existing:
                return self._row(existing, now)
            ident = uuid.uuid4().hex
            conn.execute('INSERT INTO cases(id,case_key,group_id,created_at,updated_at,received_at,state,revision,data) VALUES(?,?,?,?,?,?,?,?,?)',
                         (ident, case_key, self.group_id, now, now, received, 'pending', 1, json.dumps(data, ensure_ascii=False)))
            row = self._find(conn, ident)
            self._maintain(conn, now)
            return self._row(row, now)

    def list(self, state='pending', offset=0, limit=20, now=None):
        now = _clock(now)
        if state is not None and state not in STATES and state not in ('all', 'requested'):
            raise ValueError('复核状态格式不正确')
        if type(offset) is not int or not 0 <= offset <= 10000 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('复核页码格式不正确')
        where, args = ('WHERE group_id=?', [self.group_id])
        if state == 'requested':
            where += " AND state='pending' AND requested_action='warn'"
        elif state not in (None, 'all'):
            where += ' AND state=?'
            args.append(state)
        with self._db() as conn:
            self._maintain(conn, now)
            total = conn.execute('SELECT COUNT(*) FROM cases ' + where, args).fetchone()[0]
            rows = conn.execute('SELECT * FROM cases ' + where + ' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', args + [limit, offset]).fetchall()
            return {'items': [self._row(row, now) for row in rows], 'total': total, 'offset': offset, 'limit': limit}

    def get(self, ident, now=None):
        now = _clock(now)
        with self._db() as conn:
            self._maintain(conn, now)
            return self._row(self._find(conn, ident), now)

    def snapshot(self, now=None):
        now = _clock(now)
        with self._db() as conn:
            self._maintain(conn, now)
            counts = {state: 0 for state in STATES}
            counts.update({row['state']: row['count'] for row in conn.execute('SELECT state,COUNT(*) AS count FROM cases GROUP BY state')})
            counts['requested'] = conn.execute("SELECT COUNT(*) FROM cases WHERE state='pending' AND requested_action='warn'").fetchone()[0]
            counts['total'] = sum(counts[state] for state in STATES)
            return counts

    def request(self, ident, action, expected_revision, reason='', now=None):
        now = _clock(now)
        if action not in ('warn', 'ignore', 'correct'):
            raise ValueError('复核操作仅支持警告、忽略或误判更正')
        with self._db() as conn:
            self._maintain(conn, now)
            row = self._find(conn, ident)
            self._revision(row, expected_revision)
            if row['state'] == 'processing' or row['state'] == 'resolved':
                raise ValueError('此复核记录正在处理或已关闭')
            if action == 'warn':
                if row['state'] != 'pending' or row['requested_action'] or now - row['received_at'] >= self.warning_ttl:
                    raise ValueError('此记录已过期、结果未知或已有请求，不能再次警告')
                data = json.loads(row['data'])
                if any(conn.execute('SELECT 1 FROM action_ledger WHERE event_key=?', (key,)).fetchone() for key in data['event_keys']):
                    raise ValueError('此消息已有执行记录，不能再次警告')
                conn.execute("UPDATE cases SET requested_action='warn',requested_at=?,request_reason=?,revision=revision+1,updated_at=? WHERE id=?", (now, self._text(reason, 1000), now, ident))
            else:
                result = '人工忽略' if action == 'ignore' else '已更正误判，仅记录此案的原始内容'
                conn.execute("UPDATE cases SET state='resolved',requested_action='',requested_at=NULL,request_reason=?,result=?,revision=revision+1,updated_at=? WHERE id=?", (self._text(reason, 1000), result, now, ident))
                if action == 'correct':
                    data = json.loads(row['data'])
                    conn.execute('INSERT OR REPLACE INTO corrections(fingerprint,case_id,reason,created_at) VALUES(?,?,?,?)', (data['content_fingerprint'], ident, self._text(reason, 1000), now))
                self._maintain(conn, now)
            return self._row(self._find(conn, ident), now)

    def _claim(self, conn, row, action, now):
        if row is None or row['state'] != 'pending':
            return None
        if now - row['received_at'] >= self.warning_ttl:
            conn.execute("UPDATE cases SET requested_action='',requested_at=NULL,result='警告请求已过期，请人工忽略或更正',revision=revision+1,updated_at=? WHERE id=?", (now, row['id']))
            return None
        data = json.loads(row['data'])
        if any(conn.execute('SELECT 1 FROM action_ledger WHERE event_key=?', (key,)).fetchone() for key in data['event_keys']):
            conn.execute("UPDATE cases SET state='unknown',requested_action='',result='此消息已有执行记录，不再执行',revision=revision+1,updated_at=? WHERE id=?", (now, row['id']))
            return None
        token = uuid.uuid4().hex
        conn.execute("UPDATE cases SET state='processing',requested_action=?,claim_token=?,claim_at=?,revision=revision+1,updated_at=? WHERE id=?", (action, token, now, now, row['id']))
        for key in set(data['event_keys']):
            conn.execute('INSERT INTO action_ledger(event_key,case_id,action,claimed_at,state) VALUES(?,?,?,?,?)', (key, row['id'], action, now, 'processing'))
        return self._row(self._find(conn, row['id']), now, private=True)

    def claim(self, now=None):
        """Claim one queued owner warning. No cross-process duplicate execution."""
        now = _clock(now)
        with self._db() as conn:
            self._maintain(conn, now)
            rows = conn.execute("SELECT * FROM cases WHERE state='pending' AND requested_action='warn' ORDER BY requested_at,id").fetchall()
            for row in rows:
                claimed = self._claim(conn, row, 'warn', now)
                if claimed:
                    return claimed
            return None

    def claim_case(self, ident, action='automatic', expected_revision=None, now=None):
        """Internal bot-only durable claim BEFORE any automatic side effect."""
        now = _clock(now)
        if action not in ('automatic', 'warn', 'individual_mute'):
            raise ValueError('内部审核动作格式不正确')
        with self._db() as conn:
            self._maintain(conn, now)
            row = self._find(conn, ident)
            if expected_revision is not None:
                self._revision(row, expected_revision)
            if row is not None and row['requested_action']:
                return None
            return self._claim(conn, row, action, now)

    def finish(self, ident, state, result='', *, claim_token, expected_revision=None, now=None):
        """pending means a proven failure BEFORE sending; only it releases a claim.

        Once HTTP may have begun, use unknown on uncertainty. Unknown/resolved
        ledger entries remain durable and cannot be claimed again.
        """
        now = _clock(now)
        if state not in ('pending', 'resolved', 'unknown'):
            raise ValueError('复核完成状态格式不正确')
        with self._db() as conn:
            self._maintain(conn, now)
            row = self._find(conn, ident)
            if row is None or row['state'] != 'processing' or not claim_token or row['claim_token'] != claim_token:
                raise ValueError('复核处理凭据已失效，请刷新记录')
            if expected_revision is not None:
                self._revision(row, expected_revision)
            result = self._text(result, 2000) if isinstance(result, str) else json.dumps(self._safe(result), ensure_ascii=False)[:4000]
            conn.execute("UPDATE cases SET state=?,result=?,requested_action='',requested_at=NULL,claim_token='',claim_at=NULL,revision=revision+1,updated_at=? WHERE id=?", (state, result, now, ident))
            if state == 'pending':
                conn.execute('DELETE FROM action_ledger WHERE case_id=? AND state=\'processing\'', (ident,))
            else:
                conn.execute('UPDATE action_ledger SET state=? WHERE case_id=?', (state, ident))
            return self._row(self._find(conn, ident), now)

    def _close(self, ident, state, result, expected_revision, now):
        now = _clock(now)
        with self._db() as conn:
            self._maintain(conn, now)
            row = self._find(conn, ident)
            self._revision(row, expected_revision)
            if row['state'] != 'pending' or row['requested_action']:
                raise ValueError('此复核记录不能直接变更')
            conn.execute('UPDATE cases SET state=?,result=?,revision=revision+1,updated_at=? WHERE id=?', (state, self._text(result, 2000), now, ident))
            return self._row(self._find(conn, ident), now)

    def resolve(self, ident, result='', expected_revision=None, now=None):
        return self._close(ident, 'resolved', result, expected_revision, now)

    def note_unknown(self, ident, result='', expected_revision=None, now=None):
        return self._close(ident, 'unknown', result, expected_revision, now)

    def is_corrected(self, sources, now=None):
        """Exact-content local case annotation; caller must not treat as whitelist."""
        now = _clock(now)
        if not isinstance(sources, list) or any(not isinstance(s, dict) or not isinstance(s.get('text'), str) for s in sources):
            raise ValueError('误判更正查询格式不正确')
        with self._db() as conn:
            self._maintain(conn, now)
            return conn.execute('SELECT 1 FROM corrections WHERE fingerprint=?', (_fingerprint(sources),)).fetchone() is not None
