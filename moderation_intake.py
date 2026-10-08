"""Local selection of text for model review; never a verdict or an action.

The signals below only decide which messages deserve the model's attention.
Quotes, fiction, political debate and consensual teasing are still adjudicated
by the existing model policy. No rule here can warn, mute, or change permissions.
Only the bounded collecting/pending queues retain text; dedup/history contain hashes and
timestamps, and snapshot()/selection results contain no message contents.
"""
import collections
import copy
import hashlib
import json
import math
import re
import threading
import time
import unicodedata


DEFAULT = {
    'risk_screening': True,
    'audit_every': 50,
    'audit_interval': 120,
    'batch_limit': 12,
    'collect_enabled': True,
    'collect_quiet': 12,
    'collect_max': 45,
    'max_segments': 6,
    'max_pool_messages': 100,
    'max_collect_chars': 4000,
    'message_ttl': 120,
}
DEDUP_SECONDS = 120
SPAM_SECONDS = 30
SPAM_REPEATS = 3
MAX_PENDING = 100
MAX_HISTORY = 2000
SEGMENT_SEPARATOR = '\n[消息边界]\n'
MAX_METADATA_CHARS = 256


def validate(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError('审核筛选设置格式不正确')
    clean = {**DEFAULT, **value}
    for key in ('risk_screening', 'collect_enabled'):
        if type(clean[key]) is not bool:
            raise ValueError('审核候选筛选开关不正确')
    for key, low, high in (
        ('audit_every', 0, 10000), ('audit_interval', 0, 3600),
        ('batch_limit', 1, 20),
        ('collect_quiet', 0, 120), ('collect_max', 1, 300),
        ('max_segments', 1, 20), ('max_pool_messages', 1, 1000),
        ('max_collect_chars', 64, 32000), ('message_ttl', 1, 3600),
    ):
        if type(clean[key]) is not int or not low <= clean[key] <= high:
            raise ValueError(f'{key} 超出可设置范围')
    if clean['collect_quiet'] > clean['collect_max']:
        raise ValueError('审核收集安静等待不能超过最长等待')
    if clean['collect_max'] > clean['message_ttl']:
        raise ValueError('审核最长收集等待不能超过消息有效期')
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


def _clock(value=None):
    """A malformed caller clock must never make a queue immortal."""
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return time.time()
    return value


def _received(value, now):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return now
    return min(value, now)


def _scope_key(item):
    # Labels/nicknames are intentionally absent: only the actual sender may join.
    return _hash(str(item.get('user_id')), json.dumps(
        [item.get(key) for key in ('topic', 'topic_id', 'partition', 'partition_id')],
        sort_keys=True, ensure_ascii=False, default=str))


def _safe_metadata(value):
    """Keep identifiers atomic and bounded; never keep an attached event tree."""
    return (value is None or type(value) is bool
            or type(value) is int and value.bit_length() <= 256
            or type(value) is float and math.isfinite(value)
            or type(value) is str and len(value) <= MAX_METADATA_CHARS)


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

    ingest(item) collects bounded raw turns before selecting any model work.
    enqueue(item) is the legacy immediate selector, with intake_kind/intake_reasons.
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
        self._collecting = collections.OrderedDict()
        self._anonymous_turn = 0
        self._last_now = None
        self._queue = []
        self._clean_since_audit = 0
        self._last_audit = None
        self._counts = dict.fromkeys(
            ('received', 'candidates', 'audits', 'deduplicated', 'clean',
             'empty', 'ignored_group', 'dropped', 'expired', 'batches', 'batched'), 0)

    def configure(self, config=None):
        clean = validate(config)
        with self._lock:
            self.config = clean
            # A smaller live limit must apply to already retained text too.
            self.flush_collecting(self._last_now)
            while self._collecting_messages() > clean['max_pool_messages']:
                self._flush_turn(next(iter(self._collecting)), _clock(self._last_now))
        return dict(clean)

    def __len__(self):
        with self._lock:
            return len(self._queue)

    def has_pending(self, message_ids=None, candidates_only=False, include_collecting=True):
        """Read-only pending check, including deferred retries and this group only.

        With no identifiers this also reports raw turns while collection is
        enabled, unless include_collecting=False or candidates_only=True.
        No raw queue data
        is returned or newly retained. In-flight batches are owned by the review
        worker, which must maintain its own barrier until adjudication finishes.
        """
        ids=None if message_ids is None else {str(ident) for ident in message_ids if ident is not None}
        with self._lock:
            if any((not candidates_only or row[0].get('intake_kind')=='candidate')
                   and (ids is None or any(str(ident) in ids for ident in
                        row[0].get('source_message_ids', [row[0].get('message_id')])))
                   for row in self._queue):
                return True
            return bool(include_collecting and self.config['collect_enabled']
                        and not candidates_only and any(
                            ids is None or any(str(segment.get('message_id')) in ids
                                               for segment in turn['segments'])
                            for turn in self._collecting.values()))

    def is_pending(self, message_id, candidates_only=False, include_collecting=True):
        return self.has_pending([message_id], candidates_only=candidates_only,
                                include_collecting=include_collecting)

    def clear(self):
        """Cancel pending reviews, without resetting rate/dedup protections."""
        with self._lock:
            self._queue.clear()
            self._collecting.clear()

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
        now = _clock(now)
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
            return self._screen(item, normalized, now)

    def _screen(self, item, normalized, now, *, collected=False):
        """Select a completed turn; raw collection never calls keyword rules."""
        with self._lock:
            self._prune(now)
            sender = item.get('user_id', '')
            if not collected and item.get('message_id') is not None:
                ident = _hash(sender, item['message_id'])
                if ident in self._seen_ids:
                    self._counts['deduplicated'] += 1
                    return self._decision('dedup', ('duplicate_event',))
                self._seen_ids[ident] = now
            scope = _scope_key(item)
            texts = [segment['text'] for segment in item['segments']] if collected else [item['text']]
            matching = [normalized]
            if collected:
                # Boundaries remain visible to the model. Only this same-sender,
                # same-topic turn gets the extra compact matching views.
                matching.extend((_normalize(' '.join(texts)), _normalize(''.join(texts))))
            repeats = []
            spam_repeated = False
            stamps = [segment['received_at'] for segment in item['segments']] if collected else [now]
            for text, stamp in zip(texts, stamps):
                repeated_text = _hash(scope, _normalize(text))
                repeats = [previous for previous in self._repeats.pop(repeated_text, [])
                           if stamp - SPAM_SECONDS < previous <= stamp]
                repeats.append(stamp)
                self._repeats[repeated_text] = repeats[-SPAM_REPEATS:]
                spam_repeated = spam_repeated or len(repeats) >= SPAM_REPEATS
            self._prune(now)
            fingerprint = _hash(scope, normalized, _target_key(item))
            if fingerprint in self._selected:
                self._counts['deduplicated'] += 1
                return self._decision('dedup', ('duplicate_text',))

            # Compact Chinese text also detects spacing/zero-width variants.
            matching.extend(text.replace(' ', '') for text in tuple(matching))
            reasons = []
            if self.config['risk_screening']:
                for reason, pattern in (('threat_signal', _THREAT),
                                        ('targeted_abuse_signal', _ABUSE),
                                        ('explicit_sexual_signal', _EXPLICIT),
                                        ('extreme_gore_signal', _GORE)):
                    if any(pattern.search(text) for text in matching):
                        reasons.append(reason)
                if not reasons and _has_target(item, normalized) and _INSULT.search(normalized):
                    reasons.append('targeted_abuse_signal')
                if spam_repeated or len(_URL.findall(normalized)) >= 4:
                    reasons.append('spam_signal')
            if reasons:
                self._selected[fingerprint] = now
                self._prune(now)
                self._counts['candidates'] += 1
                return self._decision('candidate', reasons)
            if not self.config['risk_screening']:
                self._selected[fingerprint] = now
                self._prune(now)
                self._counts['audits'] += 1
                return self._decision('audit', ('screening_disabled',))

            self._counts['clean'] += 1
            self._clean_since_audit += 1
            every = self.config['audit_every']
            due = self._last_audit is None or now - self._last_audit >= self.config['audit_interval']
            if every and self._clean_since_audit >= every and due:
                self._selected[fingerprint] = now
                self._prune(now)
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

    def _collecting_messages(self):
        return sum(len(turn['segments']) for turn in self._collecting.values())

    def _flush_turn(self, key, now):
        turn = self._collecting.pop(key)
        segments = turn['segments']
        ttl = self.config['message_ttl']
        live = [segment for segment in segments if now - segment['received_at'] < ttl]
        self._counts['expired'] += len(segments) - len(live)
        if not live:
            return
        # Runtime limits may have shrunk since these fragments were ingested.
        # Finish them as bounded chunks rather than emitting an oversized turn.
        chunk = []
        chars = 0
        for original in live:
            segment = {**original, 'text': original['text'][:self.config['max_collect_chars']]}
            addition = len(segment['text']) + (len(SEGMENT_SEPARATOR) if chunk else 0)
            if chunk and (len(chunk) >= self.config['max_segments']
                          or chars + addition > self.config['max_collect_chars']):
                self._screen_turn(turn, chunk, now)
                chunk = []
                chars = 0
                addition = len(segment['text'])
            chunk.append(segment)
            chars += addition
        if chunk:
            self._screen_turn(turn, chunk, now)

    def _screen_turn(self, turn, live, now):
        selected = copy.deepcopy(turn['item'])
        selected.update({
            'text': SEGMENT_SEPARATOR.join(segment['text'] for segment in live),
            'segments': copy.deepcopy(live),
            'source_message_ids': [segment['message_id'] for segment in live
                                   if segment['message_id'] is not None],
            'message_id': live[-1]['message_id'],
            'received_at': max(segment['received_at'] for segment in live),
            'first_received_at': min(segment['received_at'] for segment in live),
        })
        # Targets in an earlier fragment must survive the representative update.
        for field in ('target_user_ids', 'targets', 'mentions'):
            values = turn.get(field)
            if values:
                selected[field] = copy.deepcopy(values)
        decision = self._screen(selected, _normalize(selected['text']), now, collected=True)
        if decision['selected']:
            selected.update(intake_kind=decision['kind'], intake_reasons=list(decision['reasons']))
            self._append(selected, now)

    def _expire_pending(self, now):
        kept = []
        for item, not_before in self._queue:
            stamp = _received(item.get('received_at'), now)
            if now - stamp >= self.config['message_ttl']:
                self._counts['expired'] += len(item.get('segments') or [item])
                continue
            item['received_at'] = stamp
            kept.append((item, not_before))
        self._queue = kept

    def flush_collecting(self, now=None):
        """Screen due raw turns locally; return the number of turns completed.

        Quiet time is measured from receipt, while collect_max is measured from
        the first receipt and cannot be extended by a continuing conversation.
        Pool pressure, segment count and character limits also finish a turn.
        """
        now = _clock(now)
        with self._lock:
            self._last_now = now
            self._expire_pending(now)
            finished = 0
            for key, turn in tuple(self._collecting.items()):
                live = [segment for segment in turn['segments']
                        if now - segment['received_at'] < self.config['message_ttl']]
                self._counts['expired'] += len(turn['segments']) - len(live)
                if not live:
                    self._collecting.pop(key)
                    finished += 1
                    continue
                turn['segments'] = live
                chars = sum(len(segment['text']) for segment in live) + len(SEGMENT_SEPARATOR) * (len(live) - 1)
                due = (not self.config['collect_enabled']
                       or now - turn['last_at'] >= self.config['collect_quiet']
                       or now - turn['first_at'] >= self.config['collect_max']
                       or len(live) >= self.config['max_segments']
                       or chars >= self.config['max_collect_chars'])
                if due:
                    self._flush_turn(key, now)
                    finished += 1
            return finished

    def ingest(self, item, now=None):
        """Buffer a copy of nonempty raw text before any screening decision.

        The decision contains counters/selection metadata only. It never returns
        raw content or IDs. An unknown sender starts an isolated one-message turn.
        """
        now = _clock(now)
        with self._lock:
            self.flush_collecting(now)
            self._counts['received'] += 1
            if not isinstance(item, dict):
                raise ValueError('审核消息格式不正确')
            if item.get('group_id') is not None and str(item['group_id']) != self.group_id:
                self._counts['ignored_group'] += 1
                return {**self._decision('other_group'), 'queued': False, 'buffered': False}
            text = item.get('text', '')
            if not isinstance(text, str) or not text.strip() or not _normalize(text):
                self._counts['empty'] += 1
                return {**self._decision('empty'), 'queued': False, 'buffered': False}
            stamp = _received(item.get('received_at'), now)
            if now - stamp >= self.config['message_ttl']:
                self._counts['expired'] += 1
                return {**self._decision('expired'), 'queued': False, 'buffered': False}
            self._prune(now)
            if item.get('message_id') is not None:
                ident = _hash(item.get('user_id', ''), item['message_id'])
                if ident in self._seen_ids:
                    self._counts['deduplicated'] += 1
                    return {**self._decision('dedup', ('duplicate_event',)), 'queued': False, 'buffered': False}
                self._seen_ids[ident] = now
                self._prune(now)
            representative = {field: item[field] for field in (
                'group_id', 'user_id', 'message_id', 'source_id', 'topic', 'topic_id',
                'partition', 'partition_id') if field in item and _safe_metadata(item[field])}
            key = _scope_key(representative)
            invalid_scope = any(field in item and not _safe_metadata(item[field]) for field in
                                ('user_id', 'topic', 'topic_id', 'partition', 'partition_id'))
            if representative.get('user_id') in (None, '') or invalid_scope:
                self._anonymous_turn += 1
                key = _hash(key, self._anonymous_turn)
            text = text[:self.config['max_collect_chars']]
            segment = {'message_id': representative.get('message_id'),
                       'text': text, 'received_at': stamp,
                       'user_id': representative.get('user_id')}
            existing = self._collecting.get(key)
            if existing:
                new_chars = sum(len(part['text']) for part in existing['segments']) + len(text)
                new_chars += len(SEGMENT_SEPARATOR) * len(existing['segments'])
                if (len(existing['segments']) >= self.config['max_segments']
                        or new_chars > self.config['max_collect_chars']):
                    self._flush_turn(key, now)
            while self._collecting_messages() >= self.config['max_pool_messages']:
                self._flush_turn(next(iter(self._collecting)), now)
            # Store only the finite metadata needed for review/attribution; the
            # original event may contain attachments or unlimited extra fields.
            if key not in self._collecting:
                self._collecting[key] = {'item': representative, 'segments': [],
                                         'first_at': now, 'last_at': now}
            turn = self._collecting[key]
            turn['item'] = representative
            turn['segments'].append(segment)
            turn['last_at'] = now
            for field in ('target_user_ids', 'targets', 'mentions'):
                incoming = item.get(field)
                if incoming:
                    values = incoming if isinstance(incoming, (list, tuple)) else [incoming]
                    retained = turn.setdefault(field, [])
                    for value in values[:self.config['max_segments']]:
                        if (_safe_metadata(value) and value not in retained
                                and len(retained) < self.config['max_segments']):
                            retained.append(value)
            self.flush_collecting(now)
            return {**self._decision('collecting'), 'queued': False, 'buffered': True}

    def enqueue(self, item, now=None):
        now = _clock(now)
        with self._lock:
            decision = self.consider(item, now)
            decision['queued'] = False
            if decision['selected']:
                selected = {**item, 'intake_kind': decision['kind'],
                            'intake_reasons': list(decision['reasons']),
                            'received_at': _received(item.get('received_at'), now)}
                decision['queued'] = self._append(selected, now)
            return decision

    def take_batch(self, now=None, limit=None):
        now = _clock(now)
        with self._lock:
            self.flush_collecting(now)
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
        not_before = _clock(not_before)
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
                safe_item = {**item, 'received_at': _received(item.get('received_at'),
                                                              _clock(self._last_now))}
                if self._append(safe_item, not_before):
                    present.add(key)
                    restored += 1
            return restored

    def snapshot(self):
        """Counters/settings only, suitable for a panel or log without raw text."""
        with self._lock:
            return {**self._counts, 'pending': len(self._queue),
                    'collecting_messages': self._collecting_messages(),
                    'collecting_turns': len(self._collecting),
                    'pending_candidates': sum(row[0]['intake_kind'] == 'candidate' for row in self._queue),
                    'pending_audits': sum(row[0]['intake_kind'] == 'audit' for row in self._queue),
                    **self.config}
