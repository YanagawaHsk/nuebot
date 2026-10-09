"""Local message grouping with optional, per-group temporal topic boundaries."""
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
        self.last_partition_expired = 0
        self.partitions = {}
        self.current_partition = None
        self._partition_sequence = 0

    @staticmethod
    def _time_policy(policy):
        policy = policy or {}
        return {'enabled': bool(policy.get('time_partition_enabled', False)),
                'gap': policy.get('partition_gap', 90),
                'span': policy.get('partition_span', 300),
                'reference_age': policy.get('reference_age', 180)}

    def _new_partition(self, stamp, cause):
        ident = uuid.uuid4().hex
        self._partition_sequence += 1
        self.partitions[ident] = {'start': stamp, 'last': stamp, 'cause': cause,
                                  'sequence': self._partition_sequence}
        return ident

    def _historical_partition(self, stamp, gap, span):
        # Match event time, never arrival order. A delayed history event cannot
        # rewind the currently active partition or extend its human activity.
        matches = [(state['start'], ident) for ident, state in self.partitions.items()
                   if state['start'] <= stamp <= state['last'] + gap
                   and (stamp <= state['last'] or stamp - state['start'] < span)]
        return max(matches)[1] if matches else None

    def message_partition(self, message):
        """Look up a pending/context message's partition without exposing text."""
        return (message.get('partition')
                or self.messages.get(str(message.get('id')), {}).get('partition')
                or self.threads.get(message.get('topic'), {}).get('partition', ''))

    def partition_current(self, partition, policy, now=None):
        """A partition becomes inactive after a real pause, without new input."""
        settings = self._time_policy(policy)
        if not settings['enabled'] or not partition:
            return True  # Synthetic/legacy metadata remains compatible.
        now = time.time() if now is None else now
        state = self.partitions.get(partition)
        return bool(partition == self.current_partition and state
                    and now - state['last'] < settings['gap'])

    def is_current(self, message, policy, now=None):
        return self.partition_current(self.message_partition(message), policy, now)

    def snapshot(self, policy, now=None):
        """Return anonymous partition counts and ages, never messages or users."""
        now = time.time() if now is None else now
        settings = self._time_policy(policy)
        current = self.partitions.get(self.current_partition)
        counts = collections.Counter(row.get('partition') for row in self.messages.values()
                                     if row.get('human', False))
        recent = []
        for ident, state in sorted(self.partitions.items(), key=lambda row: row[1]['start'], reverse=True)[:8]:
            recent.append({'sequence': state['sequence'], 'cause': state['cause'],
                           'age_seconds': math.ceil(max(0, now - state['start'])),
                           'idle_seconds': math.ceil(max(0, now - state['last'])),
                           'duration_seconds': math.ceil(max(0, state['last'] - state['start'])),
                           'messages': counts[ident],
                           'active': bool(settings['enabled'] and ident == self.current_partition and
                                          self.partition_current(ident, policy, now))})
        return {'enabled': settings['enabled'],
                'active': bool(settings['enabled'] and current and
                               self.partition_current(self.current_partition, policy, now)),
                'partitions': len(self.partitions),
                'current_messages': counts[self.current_partition] if current else 0,
                'current_age_seconds': math.ceil(max(0, now - current['start'])) if current else 0,
                'idle_seconds': math.ceil(max(0, now - current['last'])) if current else 0,
                'partition_gap': settings['gap'], 'partition_span': settings['span'],
                'reference_age': settings['reference_age'], 'recent_partitions': recent}

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
        partitions = {row.get('partition') for row in self.messages.values()} | {self.current_partition}
        self.partitions = {key: value for key, value in self.partitions.items() if key in partitions}

    def remember(self, ident, topic=None, stamp=None, turn=None, partition=None):
        """Register a bot reply without treating it as a new human turn."""
        topic = self.current if topic is None else topic
        turn = turn or self.threads.get(topic, {}).get('current_turn')
        # A late bot echo belongs to the original human partition. Its send
        # timestamp must never resurrect that partition or reset the idle clock.
        partition = self.threads.get(topic, {}).get('partition', '') if partition is None else partition
        if ident is not None:
            self.messages[str(ident)] = {'topic': topic, 'stamp': time.time() if stamp is None else stamp,
                                        'turn': turn, 'partition': partition, 'human': False}
            self.messages.move_to_end(str(ident))
            self._prune()
        return topic

    def observe(self, ident, uid, text, stamp, reply_to=None, history=False, gap=120, actionable=True, turn_gap=None,
                time_partition_enabled=False, partition_gap=90, partition_span=300, reference_age=180, now=None,
                partition_pause=20):
        known = self.messages.get(str(ident)) if ident is not None else None
        if known is not None:
            return known['topic'], known.get('revision', self.threads.get(known['topic'], {}).get('revision', 0))
        reference = self.messages.get(str(reply_to)) if reply_to is not None else None
        change = bool(re.match(r'^\s*(?:@鵺\s*)?(?:换个话题|另外问(?:一下)?|不说这个了|先不聊这个|另一个问题)', text))
        partition = ''
        detached_reference = False
        live = stamp >= self.last_human and (not history or not time_partition_enabled)
        if time_partition_enabled:
            current = self.partitions.get(self.current_partition)
            if live:
                cause = ('initial' if not current else 'topic_change' if change else
                         'gap' if stamp - current['last'] >= partition_gap else
                         'span' if stamp - current['start'] >= partition_span
                         and stamp - current['last'] >= partition_pause else None)
                partition = self._new_partition(stamp, cause) if cause else self.current_partition
            else:
                partition = None if change else self._historical_partition(stamp, partition_gap, partition_span)
                partition = partition or self._new_partition(stamp, 'history')
            # Only a recent quote inside this time window may merge turns. An
            # old quotation is still context for this *new* message, not a target.
            if reference and (reference.get('partition') != partition
                              or not 0 <= stamp - reference['stamp'] <= reference_age):
                detached_reference = True
                reference = None
            if not live and partition != self.current_partition:
                # Archive timing describes observed event ranges, allowing a
                # historical sequence to group against its real latest event.
                # Never update the current human activity clock from history.
                archive = self.partitions[partition]
                archive['start'] = min(archive['start'], stamp)
                archive['last'] = max(archive['last'], stamp)
        topic = None
        if not change and not detached_reference:
            if reference:
                topic = reference['topic']
            elif (self.current is not None and 0 <= stamp - self.last_human <= gap
                  and (not time_partition_enabled or self.threads.get(self.current, {}).get('partition') == partition)):
                topic = self.current
            elif stamp < self.last_human:
                # Delayed history must not attach an old message to a newer topic.
                prior = [row for row in self.messages.values() if 0 <= stamp - row['stamp'] <= gap
                         and (not time_partition_enabled or row.get('partition') == partition)]
                if prior:
                    topic = max(prior, key=lambda row: row['stamp'])['topic']
        if topic is None:
            topic = uuid.uuid4().hex
        state = self.threads.setdefault(topic, {'revision': 0, 'last': stamp, 'speaker': uid,
                                               'turns': {}, 'speakers': {}, 'partition': partition})
        # A topic supplies shared context; a turn supplies the reply target.
        # Unquoted chatter from another person must not continually cancel an @.
        turn = reference.get('turn') if reference and reference['topic'] == topic and not change else None
        if not turn:
            turn = state['speakers'].get(str(uid))
            previous = state['turns'].get(turn)
            if previous and stamp - previous['last'] >= (gap if turn_gap is None else turn_gap):
                turn = None
        turn = turn or uuid.uuid4().hex
        if not (history and time_partition_enabled):
            state['speakers'][str(uid)] = turn
        target = state['turns'].setdefault(turn, {'revision': 0, 'last': stamp})
        if actionable and (not history or (not time_partition_enabled and stamp >= state['last'])):
            state['revision'] += 1
            target['revision'] += 1
            target['last'] = max(stamp, target['last'])
        if stamp >= state['last'] and not (history and time_partition_enabled):
            state.update(last=stamp, speaker=uid, current_turn=turn)
        if live:
            self.current = topic
            self.last_human = stamp
            if time_partition_enabled:
                self.current_partition = partition
                self.partitions[partition]['last'] = max(stamp, self.partitions[partition]['last'])
        if ident is not None or time_partition_enabled:
            # Some transports omit an id. Preserve bounded anonymous timing
            # metadata so pruning cannot turn an old topic into "unknown" and
            # accidentally let it bypass partition checks; it is never a target.
            record = str(ident) if ident is not None else 'anonymous:' + uuid.uuid4().hex
            self.messages[record] = {'topic': topic, 'stamp': stamp, 'revision': state['revision'],
                                        'turn': turn, 'turn_revision': target['revision'],
                                        'partition': partition, 'human': True}
            self.messages.move_to_end(record)
        self._prune()
        return topic, state['revision']

    def turn(self, message):
        return message.get('turn') or self.messages.get(str(message.get('id')), {}).get('turn', '')

    def stamp(self, batch, ttl):
        if not batch:
            return {'chain': uuid.uuid4().hex, 'expires': time.time() + ttl,
                    'topic': '', 'revision': 0, 'targets': [], 'partition': ''}
        latest = max(batch, key=lambda message: message['time'])
        partition = self.message_partition(latest)
        if partition:
            batch = [message for message in batch if self.message_partition(message) == partition]
        topic = latest.get('topic', '')
        turn = self.turn(latest)
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
                'partition': partition,
                'mentioned': any(message.get('mentioned') for message in batch)}

    def valid(self, stamp, now=None, policy=None):
        now = time.time() if now is None else now
        if now >= stamp.get('expires', 0):
            return False
        if not self.is_current(stamp, policy, now):
            return False
        topic = stamp.get('topic')
        if stamp.get('turn'):
            target = self.threads.get(topic, {}).get('turns', {}).get(stamp['turn'])
            return bool(target and target['revision'] == stamp.get('turn_revision'))
        return not topic or (topic in self.threads and self.threads[topic]['revision'] == stamp.get('revision'))

    def collect(self, pending, policy, now=None, ordinary_not_before=0, mention_not_before=0):
        now = time.time() if now is None else now
        buckets = {}
        self.last_expired = 0
        self.last_partition_expired = 0
        for message in pending:
            if self.is_current(message, policy, now):
                buckets.setdefault((self.message_partition(message), message.get('topic', ''), self.turn(message)), []).append(message)
            else:
                self.last_expired += 1
                self.last_partition_expired += 1
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
        pending.extend(message for message in rows if (self.message_partition(message), message.get('topic', ''), self.turn(message)) != key)
        return batch, {'phase': 'ready', 'remaining': 0, 'messages': len(batch)}

    def expire(self, pending, policy, now=None):
        """Discard stale turns even while connection or account limits block work."""
        now = time.time() if now is None else now
        self.last_partition_expired = sum(not self.is_current(message, policy, now) for message in pending)
        latest = {}
        for message in pending:
            key = (self.message_partition(message), message.get('topic', ''), self.turn(message))
            latest[key] = max(latest.get(key, 0), message['time'])
        kept = [message for message in pending if self.is_current(message, policy, now)
                and now - latest[(self.message_partition(message), message.get('topic', ''), self.turn(message))] < policy['reply_ttl']]
        removed = len(pending) - len(kept)
        pending.clear(); pending.extend(kept)
        return removed
