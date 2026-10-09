"""Bounded local diagnostics; never expose raw log fields, messages or secrets."""
import collections
from contextlib import closing
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import urllib.error
import model_diagnostics

ROOT = Path(__file__).resolve().parent
LOG_BYTES = 2 * 1024 * 1024
STAGES = ('received', 'queued', 'triggered', 'skipped', 'generated', 'confirmed',
          'send_failed', 'send_unknown', 'expired')
STAGE_UNITS = {name: ('jobs' if name in ('triggered', 'generated') else
                     'onebot_messages' if name in ('confirmed', 'send_failed', 'send_unknown')
                     else 'messages') for name in STAGES}
REASONS = {
    'duplicate', 'self_message', 'wrong_group', 'unsupported_message', 'empty_message',
    'chat_disabled', 'paused', 'quiet', 'mention_only', 'probability', 'collecting',
    'incomplete', 'cooldown', 'message_budget', 'model_budget', 'account_budget',
    'model_queue', 'model_cooldown', 'no_intent', 'empty_reply', 'security_input',
    'security_output', 'reply_expired', 'superseded', 'connection_unavailable',
    'disconnected', 'stopped', 'plugin_limit', 'api_failure', 'unknown_outcome',
    'received', 'queued', 'ready', 'ok', 'generated', 'sent', 'expired',
    'unknown_reason',
    'HumanMessage', 'AcceptedMention', 'AcceptedMessage', 'QueueCapacity',
    'QuietMode', 'WaitingConnection', 'ReplyCooldown', 'HourlyBudget',
    'OwnerControl', 'SecurityInputBlocked', 'ChallengeFiltered', 'MentionOnly',
    'MentionProbability', 'ReplyExpired', 'ProactiveTopic', 'PluginCommand',
    'FixedReply', 'MentionReply', 'OrdinaryReply', 'PluginResult', 'ModelReply',
    'ModelSilent', 'SecurityOutputBlocked', 'CatchphraseFiltered', 'Superseded',
    'RetryScheduled', 'PluginFailure', 'ModelFailure', 'OneBotRejected',
    'RetryableBeforeSend', 'UnknownDelivery', 'OneBotConfirmed',
    'ReconciledConfirmed',
    'OutputQueued', 'model_reservoir', 'model_background',
}
_CODES = {
    'HTTPError', 'UnknownError', 'TypeError', 'ValueError', 'KeyError', 'OSError',
    'PermissionError', 'FileNotFoundError', 'TimeoutError', 'ConnectionError',
    'ConnectionRefusedError', 'ConnectionResetError', 'ConnectionAbortedError',
    'ConnectionClosedOK', 'ConnectionClosedError', 'InvalidStatus', 'InvalidMessage',
    'URLError', 'JSONDecodeError', 'OperationalError', 'DatabaseError', 'StorageError',
    'Hourly model budget exhausted', 'DeliveryQueueFull', 'DeliveryTooLarge',
    'ModelQueueFull', 'ModelQueueTimeout', 'ModelRequestTimeout', 'ModelResponseTooLarge',
    'ReplySuperseded', 'ReplyExpired', 'Disconnected', 'MessageLimit', 'EarlierMessageUnsent',
    'InterruptedBeforeSend', 'RetryableBeforeSend', 'OneBotRejected', 'InterruptedSend',
    'UnknownOutcome', 'EmptyReply', 'RedirectRejected', 'ProbeRateLimited',
    'ModelMissingChoices', 'ModelInvalidJSON', 'ModelInvalidSchema', 'ModelOutputTruncated',
    'ModelReservoirTimeout', 'ModelInputTooLarge', 'OutputQueued', 'OutputQueueFull',
    'GroupDisabled', 'MentionOnly', 'PluginDisabled', 'StickerDisabled',
    'InputBudgetExceeded', 'InputReservoirTooSmall', 'OutputReservoirTooSmall',
}
_EVENTS = {
    'chat_paused', 'chat_resumed', 'connection_error', 'delivery_queue_error', 'fatal', 'heartbeat_error',
    'memory_learned', 'memory_learning_error', 'memory_learning_retry',
    'memory_learning_storage_error', 'memory_learning_worker_error', 'message_sent',
    'model_error', 'model_retry', 'moderation_error', 'moderation_missing_permission',
    'moderation_mute', 'moderation_unknown', 'moderation_warning', 'owner_stop',
    'plugin_error', 'plugin_sent', 'runner_started', 'runner_stopped',
    'security_input_blocked', 'security_learning_blocked', 'security_learning_filtered',
    'security_output_blocked', 'send_error', 'send_unknown', 'settings_applied',
    'settings_error', 'stale_reply_discarded', 'websocket_connected', 'websocket_error',
    'reply_health', 'reply_telemetry_ready',
    'model_wait', 'memory_learning_wait', 'moderation_wait', 'moderation_retry', 'model_request', 'model_request_finished',
    'output_queued', 'model_input_budget', 'model_provider_error',
} | {'reply_' + stage for stage in STAGES}
_LINE = re.compile(r'^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)\s+([a-z][a-z0-9_]{0,79})\s+(.*)$')
_PROBE_LOCK = threading.Lock()
PROBE_TTL = 300


def safe_code(value):
    return value if isinstance(value, str) and (value in _CODES or re.fullmatch(r'HTTP[1-5]\d\d', value)) else 'UnknownError'


def reason_key(value):
    return value if isinstance(value, str) and value in REASONS else 'unknown_reason'


def fields(exc):
    result = {'type': type(exc).__name__}
    if isinstance(exc, urllib.error.HTTPError):
        result.update(model_diagnostics.error_fields(exc))
        result['code'] = 'HTTP' + str(result['http_status']) if 'http_status' in result else 'HTTPError'
    elif safe_code(getattr(exc,'code',None)) != 'UnknownError':
        result['code'] = safe_code(exc.code)
    elif str(exc) in _CODES - {'UnknownError'}:
        result['code'] = str(exc)
    return result


def suggestion(code, provider_class=None):
    code = safe_code(code)
    provider_advice = {
        'rate_requests': '服务明确返回请求频率受限；等待共用队列退避，并降低所有群合计的调用频率。',
        'rate_tokens': '服务明确返回令牌速率受限；缩短输入或输出预算，并等待共用队列恢复。',
        'quota': '服务明确返回额度或余额不足；查看服务商账号状态，本地令牌缓冲不会增加服务额度。',
        'capacity': '服务明确返回暂时过载；等待服务恢复，旧回复过期后会放弃。',
    }
    if type(provider_class) is str and provider_class in provider_advice:
        return provider_advice[provider_class]
    if code == 'HTTP429':
        return '服务拒绝了本次请求，可能是速率限制或服务配额；先等待退避，再查看服务商后台限制。仅凭429不能判定余额不足。'
    if code == 'HTTPError':
        return '旧日志没有HTTP状态码，无法区分限流、认证、模型设置或服务异常；请查看带时间的模型短回复测试结果。'
    if code in ('HTTP401', 'HTTP403'):
        return '核对已保存密钥是否有效，以及该服务和模型的访问权限；不要在日志或聊天中发送密钥。'
    if code in ('HTTP400', 'HTTP404', 'HTTP422'):
        return '核对API最终地址、模型名称和请求格式；模型列表能读取不代表生成请求一定成功。'
    if re.fullmatch(r'HTTP5\d\d', code):
        return '模型服务出现临时故障，请等待退避后再试，避免连续手动测试增加请求。'
    if code in ('ModelQueueFull', 'ModelQueueTimeout', 'ModelReservoirTimeout', 'Hourly model budget exhausted'):
        return '请求正在受共享排队或额度保护限制；查看等待原因，增加触发概率不能解除限制。'
    if code in ('ModelInputTooLarge', 'InputBudgetExceeded'):
        return '固定说明和本轮必要输入仍超过本地输入上限；调整字符预算或缩短固定说明后再处理新消息。'
    if code == 'InputReservoirTooSmall':
        return '单次必要输入的保守令牌预估超过本地输入缓冲容量；缩短输入或提高输入缓冲容量。'
    if code == 'OutputReservoirTooSmall':
        return '单次输出预算超过本地输出缓冲容量；降低输出上限或提高输出缓冲容量。'
    if code == 'OutputQueued':
        return '回复已经生成并进入本地发送队列；后台按本群节奏和额度发送，仍会检查有效期。'
    if code == 'OutputQueueFull':
        return '本群已有较多待发送回复；先等待已有内容按节奏发送，过期内容会放弃。'
    if code in ('TimeoutError', 'ModelRequestTimeout', 'URLError'):
        return '请求连接或读取超时；核对运行机器的网络与模型服务，并查看最新测试时间。'
    if code in ('Disconnected', 'ConnectionRefusedError', 'ConnectionClosedOK', 'ConnectionClosedError', 'InvalidStatus', 'InvalidMessage'):
        return 'QQ消息连接未就绪；在运行机器核对QQ登录、OneBot配置和本机3000/3001端口。'
    if code in ('UnknownOutcome', 'InterruptedSend'):
        return '发送结果不明确；请在QQ人工核对，不能自动重发或据此认定未发送。'
    if code in ('ReplyExpired', 'ReplySuperseded'):
        return '回复已过期或被新消息替代，已放弃旧内容；不要强行补发脱离当前话题的回复。'
    if code in ('PermissionError', 'StorageError', 'OperationalError', 'DatabaseError'):
        return '检查本地文件权限、文件占用和可用空间，保留现有配置和数据。'
    if code == 'RedirectRejected':
        return 'API返回了重定向，请在连接设置填写实际最终地址；不会把密钥转发到新地址。'
    if code in ('EmptyReply','ModelMissingChoices','ModelInvalidJSON','ModelInvalidSchema','ModelOutputTruncated'):
        return '模型接口没有返回可公开发送的短回复；请核对模型与思考设置的兼容性。'
    return '这是安全错误分类；请结合QQ连接、等待原因和最新模型测试判断，不回显供应商原始报错。'


def _category(event):
    return ('memory' if 'learning' in event else 'send' if 'send' in event or 'delivery' in event or event == 'output_queued'
            else 'model' if 'model' in event else 'connection' if 'websocket' in event or event == 'connection_error'
            else 'plugin' if 'plugin' in event else 'moderation' if 'moderation' in event else 'other')


def _is_error(event, data):
    if event == 'model_provider_error':
        # The matching model_error owns the failure count. This event carries
        # only safe metadata and must not duplicate the same provider failure.
        return False
    return (any(word in event for word in ('error', 'fatal', 'unknown', 'retry', 'failed'))
            or event in ('stale_reply_discarded', 'moderation_missing_permission','model_wait','memory_learning_wait','moderation_wait')
            or event == 'reply_health' and data.get('stage') in ('send_failed', 'send_unknown', 'expired','skipped'))


def _read(group):
    group = int(group)
    if not 10000 <= group <= 999999999999:
        raise ValueError('群号不正确')
    directory = ROOT / 'group-workers' / str(group)
    rows = []
    source = {'available': False, 'files_read': 0, 'truncated': False, 'malformed_lines': 0, 'read_errors': 0}
    for filename in ('events.log.2', 'events.log.1', 'events.log'):
        path = directory / filename
        try:
            with path.open('rb') as stream:
                source['available'] = True
                stream.seek(0, 2)
                size = stream.tell()
                start = max(0, size - LOG_BYTES)
                source['truncated'] |= start > 0
                stream.seek(start)
                raw = stream.read(LOG_BYTES)
                if start:
                    raw = raw.partition(b'\n')[2]
                source['files_read'] += 1
        except FileNotFoundError:
            continue
        except OSError:
            source['read_errors'] += 1
            continue
        for line in raw.decode('utf-8', errors='replace').splitlines():
            try:
                match = _LINE.fullmatch(line)
                if not match:
                    raise ValueError('line')
                stamp, event, payload = match.groups()
                instant = datetime.fromisoformat(stamp.replace(',', '.')).timestamp()
                data = json.loads(payload)
                if not isinstance(data, dict):
                    raise ValueError('data')
                rows.append((instant, stamp, event, data))
            except (ValueError, TypeError, OverflowError, RecursionError):
                source['malformed_lines'] += 1
    rows.sort(key=lambda row: row[0])
    return rows, source


def entries(group, offset=0, category='all'):
    if type(offset) is not int or not 0 <= offset <= 10000:
        raise ValueError('页码不正确')
    if category not in ('all', 'model', 'memory', 'send', 'connection', 'plugin', 'moderation', 'other'):
        raise ValueError('日志分类不正确')
    rows, source = _read(group)
    result = []
    for _, stamp, event, data in rows:
        if not _is_error(event, data):
            continue
        kind = _category(event)
        if category != 'all' and category != kind:
            continue
        code = safe_code(data.get('code') or data.get('reason') or data.get('type',
                         'ReplySuperseded' if event == 'stale_reply_discarded' else 'UnknownError'))
        wait = data.get('wait_seconds')
        if type(wait) not in (int, float) or not math.isfinite(wait) or not 0 <= wait <= 3600:
            wait = None
        provider = _provider_fields(data)
        result.append({'time': stamp, 'event': event if event in _EVENTS else 'other_event',
                       'category': kind, 'code': code, 'retry_seconds': wait,
                       'outcome':'waiting' if event.endswith(('_wait','_retry')) else 'expired' if event=='stale_reply_discarded' or data.get('stage')=='expired' else 'silent' if event=='reply_health' and data.get('stage')=='skipped' and data.get('reason_key') not in ('ModelFailure','PluginFailure') else 'failure',
                       'reason_key':reason_key(data.get('reason_key')),
                       'purpose':data.get('purpose') if data.get('purpose') in ('mention','chat','topic','learning','moderation') else None,
                       'attempt':_number(data.get('attempt'),5),'queue_waits':_number(data.get('queue_waits'),32),
                       'duration_ms':_number(data.get('duration_ms'),3600000),
                       'suggestion': suggestion(code, provider.get('provider_class')),
                       'missing_http_status': code == 'HTTPError', **provider})
    result.reverse()
    return {'entries': result[offset:offset + 50], 'total': len(result), 'offset': offset,
            'truncated': source['truncated'], 'source': source}


def _iso(instant):
    return datetime.fromtimestamp(instant, timezone.utc).isoformat(timespec='seconds')


def _number(value, maximum=1000000):
    return value if type(value) is int and 0 <= value <= maximum else None


def _provider_fields(data):
    """Filter provider metadata again when reading potentially old local logs."""
    out = {}
    provider_class = data.get('provider_class')
    if type(provider_class) is str and provider_class in ('rate_requests', 'rate_tokens', 'quota', 'capacity', 'unknown'):
        out['provider_class'] = provider_class
    status = data.get('http_status')
    if type(status) is int and 100 <= status <= 599:
        out['http_status'] = status
    out.update(model_diagnostics.response_fields({}, {'x-request-id': data.get('request_id')}))
    delay = data.get('retry_after_seconds')
    if type(delay) in (int, float):
        try:
            if math.isfinite(delay) and 0 <= delay <= 3600:
                out['retry_after_seconds'] = float(delay)
        except (OverflowError, ValueError):
            pass
    for name in ('usage_prompt_tokens', 'usage_completion_tokens', 'usage_total_tokens', 'input_chars'):
        value = _number(data.get(name), 1_000_000_000)
        if value is not None:
            out[name] = value
    return out


def _queue(group):
    result = {'available': False, 'total': 0, 'states': {}, 'attempts': 0,
              'last_updated': None, 'read_only': True,
              'note': '仅统计当前保留的发送队列记录，不代表所有历史发送。'}
    path = ROOT / 'delivery-queue.sqlite'
    if not path.is_file():
        return result
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.25)) as conn:
            conn.execute('PRAGMA query_only=ON')
            rows = conn.execute('SELECT state,COUNT(*),SUM(attempts),MAX(updated) FROM deliveries WHERE group_id=? GROUP BY state', (int(group),)).fetchall()
        allowed = set(('unsent', 'failed', 'queued', 'preparing', 'pending', 'unknown', 'confirmed', 'dismissed', 'expired'))
        latest = []
        for state, count, attempts, updated in rows:
            name = state if state in allowed else 'other'
            result['states'][name] = result['states'].get(name, 0) + count
            result['total'] += count
            result['attempts'] += max(0, int(attempts or 0))
            if type(updated) in (int, float) and math.isfinite(updated):
                latest.append(updated)
        result['available'] = True
        if latest:
            result['last_updated'] = _iso(max(latest))
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        result['note'] = '发送队列暂时无法读取；没有修改、恢复或清除任何记录。'
    return result


def save_model_probe(ok, code, duration_ms, configuration, revision='', disable_thinking=None):
    """Store only bounded metadata from a user-requested synthetic test."""
    row = {'time': _iso(time.time()), 'ok': ok is True, 'code': safe_code(code),
           'duration_ms': max(0, min(300000, int(duration_ms))),
           'configuration': 'saved' if configuration == 'saved' else 'draft',
           'model_revision': str(revision) if str(revision).isdigit() else '',
           'disable_thinking': disable_thinking if type(disable_thinking) is bool else None}
    temporary = None
    try:
        with _PROBE_LOCK:
            previous = model_probe().get('history', [])
            data = {'history': (previous + [row])[-20:]}
            with tempfile.NamedTemporaryFile(dir=ROOT, prefix='model-diagnostics-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
            os.replace(temporary, ROOT / 'model-diagnostics.json')
        return True
    except OSError:
        return False
    finally:
        if temporary and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def model_probe(now=None, disable_thinking=None):
    now = time.time() if now is None else now
    history = []
    try:
        path = ROOT / 'model-diagnostics.json'
        if path.stat().st_size <= 32768:
            data = json.loads(path.read_text(encoding='utf-8'))
            for row in data.get('history', [])[-20:]:
                if not isinstance(row, dict):
                    continue
                instant = datetime.fromisoformat(row['time']).timestamp()
                history.append({'time': _iso(instant), 'ok': row.get('ok') is True,
                    'code': safe_code(row.get('code')), 'duration_ms': _number(row.get('duration_ms'), 300000),
                    'configuration': 'saved' if row.get('configuration') == 'saved' else 'draft',
                    'model_revision': str(row.get('model_revision', '')) if str(row.get('model_revision', '')).isdigit() else '',
                    'disable_thinking': row.get('disable_thinking') if type(row.get('disable_thinking')) is bool else None})
    except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError):
        history = []
    latest = history[-1] if history else None
    applies = False
    if (latest and latest['configuration'] == 'saved'
            and type(disable_thinking) is bool and type(latest['disable_thinking']) is bool
            and latest['disable_thinking'] == disable_thinking):
        try:
            applies = latest['model_revision'] == str((ROOT / 'model.json').stat().st_mtime_ns)
        except OSError:
            pass
    fresh = bool(latest and 0 <= now - datetime.fromisoformat(latest['time']).timestamp() <= PROBE_TTL)
    return {'available': bool(latest), 'latest': latest, 'history': history,
            'fresh': fresh, 'applies_to_saved_config': applies, 'ttl_seconds': PROBE_TTL,
            'source': 'manual_synthetic_test'}


def reply_health(group, hours=24, worker=None, now=None, disable_thinking=None):
    if type(hours) is not int or not 1 <= hours <= 168:
        raise ValueError('统计窗口需为1至168小时')
    now = time.time() if now is None else now
    start = now - hours * 3600
    all_rows, source = _read(group)
    rows = [row for row in all_rows if start <= row[0] <= now]
    markers = [row[0] for row in all_rows if row[2] == 'reply_telemetry_ready' and type(row[3].get('schema')) is int and row[3].get('schema') == 1 and row[0] <= now]
    telemetry_from = min(markers) if markers else None
    present = {name: bool(markers) for name in STAGES}
    count = collections.Counter()
    legacy = collections.Counter()
    reason_counts = {}
    events = {}
    errors = {}
    waiting = None
    source_messages = 0
    mentioned_jobs = 0
    invalid_telemetry = 0
    for instant, stamp, event, data in rows:
        stamp = _iso(instant)
        safe_event = event if event in _EVENTS else 'other_event'
        event_row = events.setdefault(safe_event, {'event': safe_event, 'count': 0, 'first_at': stamp, 'last_at': stamp})
        event_row['count'] += 1
        event_row['last_at'] = stamp
        if _is_error(event, data) and not event.endswith(('_wait','_retry')) and event!='stale_reply_discarded' and not (event=='reply_health' and data.get('stage') in ('expired','skipped')):
            code = safe_code(data.get('code') or data.get('reason') or data.get('type', 'UnknownError'))
            category = _category(event)
            provider_class = _provider_fields(data).get('provider_class')
            error = errors.setdefault((category, code, provider_class), {'category': category, 'code': code,
                'count': 0, 'first_at': stamp, 'last_at': stamp, 'suggestion': suggestion(code, provider_class),
                'missing_http_status': code == 'HTTPError',
                **({'provider_class': provider_class} if provider_class is not None else {})})
            error['count'] += 1
            error['last_at'] = stamp
        stage = data.get('stage') if event == 'reply_health' else event[6:] if event.startswith('reply_') else None
        if event == 'reply_health' and stage == 'waiting':
            waiting = {'reason_key': reason_key(data.get('reason_key')),
                       'pending': _number(data.get('pending')), 'since': stamp}
        if stage in STAGES and event in _EVENTS:
            value = _number(data.get('count', 1))
            if value is None:
                invalid_telemetry += 1
                continue
            present[stage] = True
            count[stage] += value
            key = reason_key(data.get('reason_key', 'ok'))
            bucket = reason_counts.setdefault((stage, key), {'stage': stage, 'reason_key': key, 'count': 0, 'last_at': stamp})
            bucket['count'] += value
            bucket['last_at'] = stamp
            if stage == 'triggered':
                source_messages += _number(data.get('source_count')) or 0
                mentioned_jobs += int(data.get('mentioned') is True)
        elif event == 'message_sent':
            legacy['confirmed'] += 1
        elif event in ('send_error', 'send_unknown', 'stale_reply_discarded'):
            legacy[{'send_error': 'send_failed', 'send_unknown': 'send_unknown', 'stale_reply_discarded': 'expired'}[event]] += 1
    counts = {}
    for name in STAGES:
        if present[name]:
            counts[name] = count[name]
        elif legacy[name]:
            counts[name] = legacy[name]
            present[name] = True
        else:
            counts[name] = None
    missing_http = sum(row['count'] for row in errors.values() if row['missing_http_status'])
    source['invalid_telemetry'] = invalid_telemetry
    complete = bool(markers and telemetry_from <= start and not source['truncated'] and not source['read_errors'] and not source['malformed_lines'] and not invalid_telemetry)
    notes = ['不同阶段按消息、回复任务和OneBot分条计数，不能直接相除作为回复率。']
    if not markers:
        notes.append('旧日志未完整记录接收、触发和生成阶段；未记录显示为null，不代表没有收到消息或没有触发。')
    elif not complete:
        notes.append('新增回复链路记录仅覆盖部分窗口，或保留日志不完整。')
    if missing_http:
        notes.append('历史HTTPError只有异常类型，无法从这些旧记录恢复HTTP状态码。')
    if source['truncated'] or source['read_errors'] or source['malformed_lines'] or invalid_telemetry:
        notes.append('日志尾部读取有截断、读取失败或无法解析的记录；统计不代表完整历史。')
    worker = worker if isinstance(worker, dict) else {}
    state = worker.get('state')
    if state not in ('idle', 'stopped', 'starting', 'running', 'waiting_connection', 'reconnecting', 'error', 'stopping'):
        state = 'unknown'
    fresh = worker.get('fresh') is True
    stamp = worker.get('time')
    try:
        source_time = _iso(datetime.fromisoformat(stamp).timestamp())
    except (ValueError, TypeError, OverflowError):
        source_time = None
    assessment = []
    if not fresh:
        assessment.append({'code': 'stale_worker_status', 'level': 'warning', 'message': '后台状态不是实时运行证据；请查看状态更新时间，不能据此判断当前回复量。'})
    elif state in ('waiting_connection', 'reconnecting'):
        assessment.append({'code': 'connection_blocked', 'level': 'blocked', 'message': '本群正在等待QQ消息连接；未接入时不能把没有收到消息解释成触发概率低。'})
    if missing_http:
        assessment.append({'code': 'legacy_http_status_missing', 'level': 'info', 'message': suggestion('HTTPError')})
    queue = _queue(group)
    if queue['states'].get('unknown') or queue['states'].get('pending'):
        assessment.append({'code': 'send_outcome_unknown', 'level': 'blocked', 'message': suggestion('UnknownOutcome')})
    probe = model_probe(now, disable_thinking)
    if probe['fresh'] and probe['applies_to_saved_config']:
        latest = probe['latest']
        assessment.append({'code': 'model_probe_recent_success' if latest['ok'] else latest['code'],
                           'level': 'info' if latest['ok'] else 'warning',
                           'message': '最近固定短文本测试成功，仅代表该时间点，不能证明持续可用。' if latest['ok'] else suggestion(latest['code'])})
    return {'group': int(group), 'sampled_at': _iso(now),
        'window': {'hours': hours, 'start': _iso(start), 'end': _iso(now),
                   'observed_from': _iso(rows[0][0]) if rows else None, 'observed_to': _iso(rows[-1][0]) if rows else None,
                   'telemetry_from': _iso(telemetry_from) if telemetry_from else None, 'partial': not complete},
        'source': {**source, 'kind': 'bounded_local_rotated_logs'},
        'worker': {'state': state, 'fresh': fresh, 'source_time': source_time, 'realtime': fresh},
        'counts': counts, 'stage_units': STAGE_UNITS,
        'triggered_source_messages': source_messages, 'mentioned_jobs': mentioned_jobs,
        'waiting': waiting,
        'coverage': {'complete': complete, 'stage_available': present,
                     'legacy_http_errors_without_status': missing_http, 'notes': notes},
        'reasons': sorted(reason_counts.values(), key=lambda row: (-row['count'], row['stage'], row['reason_key'])),
        'errors': sorted(errors.values(), key=lambda row: (-row['count'], row['category'], row['code'])),
        'events': sorted(events.values(), key=lambda row: (-row['count'], row['event'])),
        'deliveries': queue, 'model_probe': probe, 'assessment': assessment}
