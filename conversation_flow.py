"""Local message grouping. Reply IDs and explicit topic changes outrank time."""
import collections
import math
import re
import time
import uuid


class Flow:
    def __init__(self):
        self.messages = collections.OrderedDict()
        self.threads = {}
        self.current = None
        self.last_human = 0
        self.last_expired = 0

    def _prune(self):
        while len(self.messages) > 1000:
            self.messages.popitem(last=False)
        active = {row['topic'] for row in self.messages.values()} | {self.current}
        self.threads = {key: value for key, value in self.threads.items() if key in active}
        active_turns = collections.defaultdict(set)
        for row in self.messages.values():
            active_turns[row['topic']].add(row.get('turn'))
        for topic, state in self.threads.items():
            turns = active_turns[topic]
            turns.add(state.get('current_turn'))
            state['turns'] = {key: value for key, value in state.get('turns', {}).items() if key in turns}
            state['speakers'] = {key: value for key, value in state.get('speakers', {}).items() if value in turns}

    def remember(self, ident, topic=None, stamp=None, turn=None):
        """Register a bot reply without treating it as a new human turn."""
        topic = self.current if topic is None else topic
        turn = turn or self.threads.get(topic, {}).get('current_turn')
        if ident is not None:
            self.messages[str(ident)] = {'topic': topic, 'stamp': time.time() if stamp is None else stamp,
                                        'turn': turn}
            self.messages.move_to_end(str(ident))
            self._prune()
        return topic

    def observe(self, ident, uid, text, stamp, reply_to=None, history=False, gap=120, actionable=True, turn_gap=None):
        known = self.messages.get(str(ident)) if ident is not None else None
        if known is not None:
            return known['topic'], known.get('revision', self.threads.get(known['topic'], {}).get('revision', 0))
        reference = self.messages.get(str(reply_to)) if reply_to is not None else None
        change = bool(re.match(r'^\s*(?:@鵺\s*)?(?:换个话题|另外问(?:一下)?|不说这个了|先不聊这个|另一个问题)', text))
        topic = None
        if not change:
            if reference:
                topic = reference['topic']
            elif self.current is not None and 0 <= stamp - self.last_human <= gap:
                topic = self.current
            elif stamp < self.last_human:
                # Delayed history must not attach an old message to a newer topic.
                prior = [row for row in self.messages.values() if 0 <= stamp - row['stamp'] <= gap]
                if prior:
                    topic = max(prior, key=lambda row: row['stamp'])['topic']
        if topic is None:
            topic = uuid.uuid4().hex
        state = self.threads.setdefault(topic, {'revision': 0, 'last': stamp, 'speaker': uid,
                                               'turns': {}, 'speakers': {}})
        # A topic supplies shared context; a turn supplies the reply target.
        # Unquoted chatter from another person must not continually cancel an @.
        turn = reference.get('turn') if reference and reference['topic'] == topic and not change else None
        if not turn:
            turn = state['speakers'].get(str(uid))
            previous = state['turns'].get(turn)
            if previous and stamp - previous['last'] >= (gap if turn_gap is None else turn_gap):
                turn = None
        turn = turn or uuid.uuid4().hex
        state['speakers'][str(uid)] = turn
        target = state['turns'].setdefault(turn, {'revision': 0, 'last': stamp})
        if actionable and (not history or stamp >= state['last']):
            state['revision'] += 1
            target['revision'] += 1
            target['last'] = max(stamp, target['last'])
        if stamp >= state['last']:
            state.update(last=stamp, speaker=uid, current_turn=turn)
        if stamp >= self.last_human:
            self.current = topic
            self.last_human = stamp
        if ident is not None:
            self.messages[str(ident)] = {'topic': topic, 'stamp': stamp, 'revision': state['revision'],
                                        'turn': turn, 'turn_revision': target['revision']}
            self.messages.move_to_end(str(ident))
        self._prune()
        return topic, state['revision']

    def turn(self, message):
        return message.get('turn') or self.messages.get(str(message.get('id')), {}).get('turn', '')

    def stamp(self, batch, ttl):
        if not batch:
            return {'chain': uuid.uuid4().hex, 'expires': time.time() + ttl,
                    'topic': '', 'revision': 0, 'targets': []}
        topic = max(batch, key=lambda message: message['time']).get('topic', '')
        turn = self.turn(max(batch, key=lambda message: message['time']))
        targets = [message['id'] for message in batch if message.get('id') is not None]
        revisions = [self.messages.get(str(ident), {}).get('revision', 0) for ident in targets]
        # A message arriving after collect() must invalidate this draft, even if
        # it arrives before stamp() is called and is still in the pending queue.
        revision = max(revisions, default=self.threads.get(topic, {}).get('revision', 0))
        turn_revision = max((message.get('turn_revision', self.messages.get(str(message.get('id')), {}).get('turn_revision', 0))
                             for message in batch if self.turn(message) == turn), default=0)
        return {'chain': uuid.uuid4().hex, 'expires': max(message['time'] for message in batch) + ttl,
                'topic': topic, 'revision': revision, 'targets': targets,
                'turn': turn, 'turn_revision': turn_revision, 'source_count': len(batch),
                'mentioned': any(message.get('mentioned') for message in batch)}

    def valid(self, stamp, now=None):
        now = time.time() if now is None else now
        if now >= stamp.get('expires', 0):
            return False
        topic = stamp.get('topic')
        if stamp.get('turn'):
            target = self.threads.get(topic, {}).get('turns', {}).get(stamp['turn'])
            return bool(target and target['revision'] == stamp.get('turn_revision'))
        return not topic or (topic in self.threads and self.threads[topic]['revision'] == stamp.get('revision'))

    def collect(self, pending, policy, now=None, ordinary_not_before=0, mention_not_before=0):
        now = time.time() if now is None else now
        buckets = {}
        for message in pending:
            buckets.setdefault((message.get('topic', ''), self.turn(message)), []).append(message)
        self.last_expired = 0
        rows = []
        alive = {}
        for key, batch in buckets.items():
            # Keep the question/@ anchor while genuine continuations remain fresh.
            # Other people's unrelated turns cannot extend this lifetime.
            if now - max(message['time'] for message in batch) >= policy['reply_ttl']:
                self.last_expired += len(batch)
            else:
                alive[key] = batch
                rows.extend(batch)
        pending.clear()
        pending.extend(rows)
        candidates = []
        remaining = []
        for key, batch in alive.items():
            batch.sort(key=lambda message: (message['time'], self.messages.get(str(message.get('id')), {}).get('revision', 0)))
            first, last = batch[0]['time'], batch[-1]['time']
            unfinished = bool(re.search(r'(?:[，、：…]|\.\.\.|然后|但是|因为|比如|我还没说完|等我|我接着说)\s*$', batch[-1]['text']))
            quiet = policy['collect_incomplete'] if unfinished else policy['collect_quiet']
            due = last + quiet
            # The long-window limit still requires a real pause. A new turn
            # invalidates the draft while it is queued, generated or delayed.
            if not unfinished:
                due = min(due, max(first + policy['collect_max'], last + min(8, quiet)))
            retry_due = max((message.get('retry_at', 0) for message in batch), default=0)
            retrying = retry_due > now and retry_due >= due
            due = max(due, retry_due)
            mentioned = any(message.get('mentioned') for message in batch)
            not_before = mention_not_before if mentioned else ordinary_not_before
            cooled = not_before > now and not_before >= due
            due = max(due, not_before)
            if due <= now:
                candidates.append((not mentioned, first, key, batch))
            else:
                remaining.append((due - now, 'cooldown' if cooled else 'retry_wait' if retrying else 'collecting'))
        if not candidates:
            wait, phase = min(remaining) if remaining else (0, 'idle')
            return None, {'phase': phase,
                          'remaining': math.ceil(wait),
                          'messages': len(rows)}
        _, _, key, batch = min(candidates, key=lambda value: value[:2])
        pending.clear()
        pending.extend(message for message in rows if (message.get('topic', ''), self.turn(message)) != key)
        return batch, {'phase': 'ready', 'remaining': 0, 'messages': len(batch)}

    def expire(self, pending, policy, now=None):
        """Discard stale turns even while connection or account limits block work."""
        now = time.time() if now is None else now
        latest = {}
        for message in pending:
            key = (message.get('topic', ''), self.turn(message))
            latest[key] = max(latest.get(key, 0), message['time'])
        kept = [message for message in pending if now - latest[(message.get('topic', ''), self.turn(message))] < policy['reply_ttl']]
        removed = len(pending) - len(kept)
        pending.clear(); pending.extend(kept)
        return removed
