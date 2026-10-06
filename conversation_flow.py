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

    def _prune(self):
        while len(self.messages) > 1000:
            self.messages.popitem(last=False)
        active = {row['topic'] for row in self.messages.values()} | {self.current}
        self.threads = {key: value for key, value in self.threads.items() if key in active}

    def remember(self, ident, topic=None, stamp=None):
        """Register a bot reply without treating it as a new human turn."""
        topic = self.current if topic is None else topic
        if ident is not None:
            self.messages[str(ident)] = {'topic': topic, 'stamp': time.time() if stamp is None else stamp}
            self.messages.move_to_end(str(ident))
            self._prune()
        return topic

    def observe(self, ident, uid, text, stamp, reply_to=None, history=False, gap=120):
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
        state = self.threads.setdefault(topic, {'revision': 0, 'last': stamp, 'speaker': uid})
        if not history or stamp >= state['last']:
            state['revision'] += 1
        if stamp >= state['last']:
            state.update(last=stamp, speaker=uid)
        if stamp >= self.last_human:
            self.current = topic
            self.last_human = stamp
        if ident is not None:
            self.messages[str(ident)] = {'topic': topic, 'stamp': stamp, 'revision': state['revision']}
            self.messages.move_to_end(str(ident))
        self._prune()
        return topic, state['revision']

    def stamp(self, batch, ttl):
        if not batch:
            return {'chain': uuid.uuid4().hex, 'expires': time.time() + ttl,
                    'topic': '', 'revision': 0, 'targets': []}
        topic = max(batch, key=lambda message: message['time']).get('topic', '')
        targets = [message['id'] for message in batch if message.get('id') is not None]
        revisions = [self.messages.get(str(ident), {}).get('revision', 0) for ident in targets]
        # A message arriving after collect() must invalidate this draft, even if
        # it arrives before stamp() is called and is still in the pending queue.
        revision = max(revisions, default=self.threads.get(topic, {}).get('revision', 0))
        return {'chain': uuid.uuid4().hex, 'expires': max(message['time'] for message in batch) + ttl,
                'topic': topic, 'revision': revision, 'targets': targets,
                'mentioned': any(message.get('mentioned') for message in batch)}

    def valid(self, stamp, now=None):
        now = time.time() if now is None else now
        if now >= stamp.get('expires', 0):
            return False
        topic = stamp.get('topic')
        return not topic or (topic in self.threads and self.threads[topic]['revision'] == stamp.get('revision'))

    def collect(self, pending, policy, now=None):
        now = time.time() if now is None else now
        rows = [message for message in pending if now - message['time'] < policy['reply_ttl']]
        pending.clear()
        pending.extend(rows)
        buckets = {}
        for message in rows:
            buckets.setdefault(message.get('topic', ''), []).append(message)
        candidates = []
        remaining = []
        for topic, batch in buckets.items():
            batch.sort(key=lambda message: message['time'])
            first, last = batch[0]['time'], batch[-1]['time']
            unfinished = bool(re.search(r'(?:[，、：…]|\.\.\.|然后|但是|因为|比如|我还没说完|等我|我接着说)\s*$', batch[-1]['text']))
            quiet = policy['collect_incomplete'] if unfinished else policy['collect_quiet']
            due = last + quiet
            # The long-window limit still requires a real pause. A new turn
            # invalidates the draft while it is queued, generated or delayed.
            due = min(due, max(first + policy['collect_max'], last + min(8, quiet)))
            due = max(due, max((message.get('retry_at', 0) for message in batch), default=0))
            if due <= now:
                candidates.append((not any(message.get('mentioned') for message in batch), first, topic, batch))
            else:
                remaining.append(due - now)
        if not candidates:
            return None, {'phase': 'collecting' if rows else 'idle',
                          'remaining': math.ceil(min(remaining)) if remaining else 0,
                          'messages': len(rows)}
        _, _, topic, batch = min(candidates, key=lambda value: value[:2])
        pending.clear()
        pending.extend(message for message in rows if message.get('topic', '') != topic)
        return batch, {'phase': 'ready', 'remaining': 0, 'messages': len(batch)}
