"""Plan bounded retries without mistaking local queue waits for HTTP attempts."""
import math
import time
import urllib.error


def plan(batch, exc, policy, *, request_started=False, next_ready=0, budget_ready=0, now=None):
    now = time.time() if now is None else now
    request_started = bool(request_started or isinstance(exc, urllib.error.HTTPError))
    expires = max((row['time'] for row in batch), default=now) + policy['reply_ttl']
    attempts = max((row.get('model_attempts', 0) for row in batch), default=0) + int(request_started)
    waits = max((row.get('queue_waits', 0) for row in batch), default=0)
    code = getattr(exc, 'code', None)
    code = ('HTTP' + str(code)) if isinstance(exc, urllib.error.HTTPError) else code or str(exc)
    local_wait = not request_started and (type(exc).__name__ == 'QueueExpired' or code == 'Hourly model budget exhausted')
    if local_wait:
        waits += 1
        delay = max(policy['retry_base'], min(30, policy['retry_base'] * 2 ** min(waits - 1, 3)))
        hinted = getattr(exc, 'next_ready_at', None)
        hinted = hinted if type(hinted) in (int,float) and math.isfinite(hinted) else 0
        retry_at = max(now + delay, next_ready, budget_ready, hinted)
        allowed = waits <= 12
    else:
        retryable = isinstance(exc, (TimeoutError, ConnectionError, urllib.error.URLError))
        if isinstance(exc, urllib.error.HTTPError):
            retryable = exc.code == 429 or 500 <= exc.code < 600
        retry_at = max(now + policy['retry_base'] * 2 ** min(max(attempts - 1, 0), 4), next_ready)
        allowed = retryable and attempts < policy['retry_attempts']
    allowed = bool(batch and policy['auto_retry'] and allowed and retry_at < expires)
    return {'retry': allowed, 'local_wait': local_wait, 'attempts': attempts, 'queue_waits': waits,
            'retry_at': retry_at, 'wait_seconds': max(0, math.ceil(retry_at - now)),
            'expires': expires, 'expired': now >= expires or retry_at >= expires}
