"""Local selection of text for model review; never a verdict or an action.

The signals below only decide which messages deserve the model's attention.
Quotes, fiction, political debate and consensual teasing are still adjudicated
by the existing model policy. No rule here can warn, mute, or change permissions.
Only the bounded pending queue retains text; dedup/history contain hashes and
timestamps, and snapshot()/selection results contain no message contents.
"""
import collections
import copy
import hashlib
import json
import re
import threading
import time
import unicodedata


DEFAULT = {
    'risk_screening': True,
    'audit_every': 50,
    'audit_interval': 120,
    'batch_limit': 12,
}
DEDUP_SECONDS = 120
SPAM_SECONDS = 30
SPAM_REPEATS = 3
MAX_PENDING = 100
MAX_HISTORY = 2000


def validate(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError('审核筛选设置格式不正确')
    clean = {**DEFAULT, **value}
    if type(clean['risk_screening']) is not bool:
        raise ValueError('审核候选筛选开关不正确')
    for key, low, high in (
        ('audit_every', 0, 10000), ('audit_interval', 0, 3600),
        ('batch_limit', 1, 20),
    ):
        if type(clean[key]) is not int or not low <= clean[key] <= high:
            raise ValueError(f'{key} 超出可设置范围')
    return {key: clean[key] for key in DEFAULT}


_INVISIBLE = re.compile('[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]')
_URL = re.compile(r'https?://[^\s<>]+', re.I)
_THREAT = re.compile(
    r'(?:我(?:要|会|马上|现在|一定|迟早|早晚)?|老子).{0,8}'
    r'(?:弄死|杀了|杀死|砍死|打死|捅死|炸死|废了|杀|砍|捅|揍|打).{0,8}(?:你|他|她|你们|他们|她们)'
    r'|(?:你|你们).{0,6}(?:等着|等死).{0,8}(?:弄死|杀|砍|捅)'
    r'|\bi(?:\s+will|\s+am\s+going\s+to|\s+am\s+gonna|\s+gonna|\x27ll|\x27m\s+going\s+to)'
    r'\s+(?:fucking\s+)?(?:kill|murder|stab|shoot|hurt|beat\s+up)\s+(?:you|him|her|them)\b',
    re.I,
)
_ABUSE = re.compile(
    r'(?:你们?|他们?|她们?).{0,10}(?:傻[逼比]|煞笔|脑残|弱智|废物|畜生|狗东西|死全家)'
    r'|(?:去死|死全家|杀了自己|自杀吧|操你妈|草你妈)'
    r'|\byou(?:\s+are|\x27re)?(?:\s+(?:a|an|such|fucking|stupid)){0,3}'
    r'\s+(?:idiot|moron|scumbag|piece\s+of\s+shit|worthless)\b'
    r'|\b(?:kill\s+yourself|go\s+die|kys)\b',
    re.I,
)
_INSULT = re.compile(r'傻[逼比]|煞笔|脑残|弱智|废物|畜生|狗东西|\b(?:idiot|moron|scumbag)\b', re.I)
_EXPLICIT = re.compile(
    r'口交|肛交|抽插|(?:插入|舔|含住|摩擦).{0,8}(?:阴道|阴茎|龟头|阴蒂|肛门)'
    r'|(?:阴茎|龟头).{0,8}(?:插入|插进|抽插)|(?:强奸|轮奸).{0,8}(?:你|她|他)'
    r'|\b(?:blowjob|anal\s+sex|penetrat(?:e|es|ing|ion).{0,12}(?:vagina|anus))\b',
    re.I,
)
_GORE = re.compile(
    r'脑浆四溅|脑浆.{0,6}(?:流出|喷出)|肠子.{0,6}(?:流出|掏出|扯出)'
    r'|开膛破肚|活剥.{0,5}(?:人皮|皮)|(?:剁碎|肢解).{0,6}(?:尸体|人体)'
    r'|\b(?:brains\s+(?:splattered|spilling)|intestines\s+(?:spilling|pulled\s+out))\b',
    re.I,
)


def _normalize(text):
    return ' '.join(_INVISIBLE.sub('', unicodedata.normalize('NFKC', text)).casefold().split())


def _hash(*parts):
    # Do not retain raw messages, ids, or URLs in dedup/history dictionaries.
    return hashlib.sha256('\0'.join(str(part) for part in parts).encode('utf-8')).hexdigest()


def _has_target(item, text):
    return bool(
        item.get('target_user_ids') or item.get('targets') or item.get('mentions')
        or re.search(r'\[cq:at,qq=\d+\]|@\S+', text, re.I)
    )


def _target_key(item):
    return json.dumps([item.get(key) for key in ('target_user_ids', 'targets', 'mentions')],
                      sort_keys=True, ensure_ascii=False, default=str)


class ModerationIntake:
    """One group's in-memory intake, safe to use from receiver and worker threads.

    enqueue(item) screens and queues a copy with intake_kind/intake_reasons.
    take_batch() returns ready candidates first, then periodic ordinary samples.
    restore_batch(items, not_before=...) retries already selected work without
    screening it a second time. consider(item) screens without adding to a queue;
    do not call consider() and enqueue() for the same event.
    """
    def __init__(self, group_id, config=None):
        self.group_id = str(group_id)
        self.config = validate(config)
        self._lock = threading.RLock()
        self._seen_ids = collections.OrderedDict()
        self._selected = collections.OrderedDict()
        self._repeats = collections.OrderedDict()
        self._queue = []
        self._clean_since_audit = 0
        self._last_audit = None
        self._counts = dict.fromkeys(
            ('received', 'candidates', 'audits', 'deduplicated', 'clean',
             'empty', 'ignored_group', 'dropped', 'batches', 'batched'), 0)

    def configure(self, config=None):
        clean = validate(config)
        with self._lock:
            self.config = clean
        return dict(clean)

    def __len__(self):
        with self._lock:
            return len(self._queue)

    def clear(self):
        """Cancel pending reviews, without resetting rate/dedup protections."""
        with self._lock:
            self._queue.clear()

    def _prune(self, now):
        for history in (self._seen_ids, self._selected, self._repeats):
            while history:
                value = next(iter(history.values()))
                stamp = value[-1] if isinstance(value, list) else value
                if stamp > now - DEDUP_SECONDS and len(history) <= MAX_HISTORY:
                    break
                history.popitem(last=False)

    @staticmethod
    def _decision(kind, reasons=()):
        return {'selected': kind in ('candidate', 'audit'), 'kind': kind,
                'reasons': list(reasons)}

    def consider(self, item, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._counts['received'] += 1
            if not isinstance(item, dict):
                raise ValueError('审核消息格式不正确')
            if item.get('group_id') is not None and str(item['group_id']) != self.group_id:
                self._counts['ignored_group'] += 1
                return self._decision('other_group')
            text = item.get('text', '')
            if not isinstance(text, str) or not text.strip():
                self._counts['empty'] += 1
                return self._decision('empty')
            normalized = _normalize(text)
            if not normalized:
                self._counts['empty'] += 1
                return self._decision('empty')
            self._prune(now)
            sender = item.get('user_id', '')
            if item.get('message_id') is not None:
                ident = _hash(sender, item['message_id'])
                if ident in self._seen_ids:
                    self._counts['deduplicated'] += 1
                    return self._decision('dedup', ('duplicate_event',))
                self._seen_ids[ident] = now
            repeated_text = _hash(sender, normalized)
            fingerprint = _hash(sender, normalized, _target_key(item))
            repeats = [stamp for stamp in self._repeats.pop(repeated_text, [])
                       if stamp > now - SPAM_SECONDS]
            repeats.append(now)
            self._repeats[repeated_text] = repeats[-SPAM_REPEATS:]
            if fingerprint in self._selected:
                self._counts['deduplicated'] += 1
                return self._decision('dedup', ('duplicate_text',))

            # Compact Chinese text also detects spacing/zero-width variants.
            compact = normalized.replace(' ', '')
            reasons = []
            if self.config['risk_screening']:
                for reason, pattern in (('threat_signal', _THREAT),
                                        ('targeted_abuse_signal', _ABUSE),
                                        ('explicit_sexual_signal', _EXPLICIT),
                                        ('extreme_gore_signal', _GORE)):
                    if pattern.search(normalized) or pattern.search(compact):
                        reasons.append(reason)
                if not reasons and _has_target(item, normalized) and _INSULT.search(normalized):
                    reasons.append('targeted_abuse_signal')
                if len(repeats) >= SPAM_REPEATS or len(_URL.findall(normalized)) >= 4:
                    reasons.append('spam_signal')
            if reasons:
                self._selected[fingerprint] = now
                self._counts['candidates'] += 1
                return self._decision('candidate', reasons)
            if not self.config['risk_screening']:
                self._selected[fingerprint] = now
                self._counts['audits'] += 1
                return self._decision('audit', ('screening_disabled',))

            self._counts['clean'] += 1
            self._clean_since_audit += 1
            every = self.config['audit_every']
            due = self._last_audit is None or now - self._last_audit >= self.config['audit_interval']
            if every and self._clean_since_audit >= every and due:
                self._selected[fingerprint] = now
                self._last_audit = now
                self._clean_since_audit = 0
                self._counts['audits'] += 1
                return self._decision('audit', ('periodic_sample',))
            return self._decision('clean')

    def _append(self, item, not_before):
        if len(self._queue) >= MAX_PENDING:
            audit = next((index for index, row in enumerate(self._queue)
                          if row[0].get('intake_kind') == 'audit'), None)
            if item['intake_kind'] == 'audit':
                self._counts['dropped'] += 1
                return False
            self._queue.pop(audit if audit is not None else 0)
            self._counts['dropped'] += 1
        self._queue.append((copy.deepcopy(item), not_before))
        return True

    def enqueue(self, item, now=None):
        now = time.time() if now is None else now
        with self._lock:
            decision = self.consider(item, now)
            decision['queued'] = False
            if decision['selected']:
                selected = {**item, 'intake_kind': decision['kind'],
                            'intake_reasons': list(decision['reasons'])}
                decision['queued'] = self._append(selected, now)
            return decision

    def take_batch(self, now=None, limit=None):
        now = time.time() if now is None else now
        with self._lock:
            count = self.config['batch_limit']
            if limit is not None:
                if type(limit) is not int or limit < 1:
                    raise ValueError('审核批次上限不正确')
                count = min(count, limit)
            ready = [(index, row) for index, row in enumerate(self._queue) if row[1] <= now]
            ready.sort(key=lambda pair: pair[1][0]['intake_kind'] != 'candidate')
            chosen = ready[:count]
            indexes = {index for index, _ in chosen}
            self._queue = [row for index, row in enumerate(self._queue) if index not in indexes]
            items = [copy.deepcopy(row[0]) for _, row in chosen]
            if items:
                self._counts['batches'] += 1
                self._counts['batched'] += len(items)
            return items

    def restore_batch(self, items, not_before=None):
        """Restore selected work after a deferred request; return restored count."""
        not_before = time.time() if not_before is None else not_before
        with self._lock:
            present = {_hash(row[0].get('user_id'), row[0].get('message_id'), row[0].get('text'))
                       for row in self._queue}
            restored = 0
            for item in items:
                if not isinstance(item, dict) or item.get('intake_kind') not in ('candidate', 'audit'):
                    raise ValueError('只能恢复已筛选的审核消息')
                if item.get('group_id') is not None and str(item['group_id']) != self.group_id:
                    raise ValueError('不能恢复其他群的审核消息')
                key = _hash(item.get('user_id'), item.get('message_id'), item.get('text'))
                if key in present:
                    continue
                if self._append(item, not_before):
                    present.add(key)
                    restored += 1
            return restored

    def snapshot(self):
        """Counters/settings only, suitable for a panel or log without raw text."""
        with self._lock:
            return {**self._counts, 'pending': len(self._queue),
                    'pending_candidates': sum(row[0]['intake_kind'] == 'candidate' for row in self._queue),
                    'pending_audits': sum(row[0]['intake_kind'] == 'audit' for row in self._queue),
                    **self.config}
