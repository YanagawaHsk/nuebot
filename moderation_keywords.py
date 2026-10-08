"""Bounded literal rules for warning candidates; this module never sends messages.

Every match retains a span in an original source message. A clear phrase can be
warned without a model, while quotations, negation and playful/fictional context
remain review candidates. Collection, per-group cooldowns and delivery are the
caller's responsibility. These defaults do not censor political opinions or
ordinary adult/soft sexual conversation.
"""
import copy
import re
import unicodedata


CATEGORIES = ('targeted_abuse', 'threat', 'explicit_sexual')
MAX_RULES = 100
MAX_PHRASES_PER_RULE = 20
MAX_SOURCES = 20
_INVISIBLE = re.compile('[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]')
_RULE_ID = re.compile(r'[A-Za-z0-9_.-]{1,64}\Z')
_TRADITIONAL = str.maketrans({'杀': '殺', '会': '會', '奸': '姦', '强': '強',
                            '轮': '輪', '这': '這', '妈': '媽', '个': '個',
                            '该': '該', '怎': '怎', '么': '麼', '还': '還'})
_CONTEXT_SIMPLIFIED = str.maketrans({
    '殺': '杀', '會': '会', '姦': '奸', '強': '强', '輪': '轮', '這': '这',
    '媽': '妈', '個': '个', '該': '该', '麼': '么', '還': '还', '說': '说',
    '轉': '转', '詞': '词', '臺': '台', '劇': '剧', '電': '电', '視': '视',
    '遊': '游', '戲': '戏', '裡': '里', '虛': '虚', '構': '构', '關': '关',
    '鍵': '键', '庫': '库', '討': '讨', '論': '论', '學': '学', '應': '应',
    '開': '开', '鬧': '闹', '著': '着', '話': '话', '別': '别', '當': '当',
    '嚇': '吓', '脅': '胁', '罵': '骂', '願': '愿', '許': '许', '沒': '没',
})
_SECRET = re.compile(r'(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{12,}|bearer\s+\S+'
                     r'|(?:api[_ -]?key|access[_ -]?token|secret)\s*[:=]\s*\S+'
                     r'|(?:密钥|密鑰|访问令牌|訪問令牌)\s*[:：=]\s*\S+', re.I)


def _rule(ident, label, category, phrases, warning):
    variants = []
    for phrase in phrases:
        for variant in (phrase, phrase.translate(_TRADITIONAL)):
            if variant not in variants:
                variants.append(variant)
    return {'id': ident, 'enabled': True, 'category': category,
            'phrases': variants, 'mode': 'contains', 'label': label,
            'warning_text': warning}


_THREAT_WARNING = '对人的威胁已经过界啦，收一收，别继续。'
_ABUSE_WARNING = '这句对人的辱骂已经过界啦，收一收。'
_SEXUAL_WARNING = '这种针对群友的露骨骚扰就到这里，别继续啦。'

DEFAULT = {
    'enabled': True,
    'record_warning_count': False,
    'user_cooldown_seconds': 120,
    'max_text_chars': 4000,
    'rules': [
        _rule('threat_kill', '直接杀人威胁', 'threat',
              ('我要杀了你', '我会杀了你', '我一定杀了你'), _THREAT_WARNING),
        _rule('threat_murder', '直接弄死威胁', 'threat',
              ('我要弄死你', '我会弄死你', '我一定弄死你'), _THREAT_WARNING),
        _rule('threat_stab', '直接持刀威胁', 'threat',
              ('我要捅死你', '我会捅死你', '我一定捅死你'), _THREAT_WARNING),
        _rule('threat_chop', '直接砍杀威胁', 'threat',
              ('我要砍死你', '我会砍死你', '我一定砍死你'), _THREAT_WARNING),
        _rule('threat_family', '针对家人的杀害威胁', 'threat',
              ('我要杀你全家', '我会杀你全家', '我要弄死你全家'), _THREAT_WARNING),
        _rule('threat_wait', '点名后续伤害威胁', 'threat',
              ('你等着我弄死你', '你等着我杀了你', '你等着我捅死你'), _THREAT_WARNING),
        _rule('abuse_suicide', '驱使群友去死', 'targeted_abuse',
              ('你去自杀吧', '去死吧你', '你怎么还不去死'), _ABUSE_WARNING),
        _rule('abuse_mother', '针对家人的严重辱骂', 'targeted_abuse',
              ('操你妈', '草你妈'), _ABUSE_WARNING),
        _rule('abuse_target', '直接严重辱骂', 'targeted_abuse',
              ('你这个傻逼', '你真是个傻逼', '你他妈就是个傻逼'), _ABUSE_WARNING),
        _rule('abuse_family', '诅咒群友家人', 'targeted_abuse',
              ('你全家都该死', '你全家都去死', '祝你全家死光'), _ABUSE_WARNING),
        _rule('abuse_animal', '直接贬损群友', 'targeted_abuse',
              ('你这个畜生', '你就是个畜生'), _ABUSE_WARNING),
        _rule('sexual_coercion', '直接性暴力威胁', 'explicit_sexual',
              ('我要强奸你', '我会强奸你', '我要轮奸你'), _SEXUAL_WARNING),
        _rule('sexual_harassment', '针对群友的露骨骚扰', 'explicit_sexual',
              ('我要操死你', '我会操死你'), _SEXUAL_WARNING),
    ],
}


def _normalize_with_spans(text):
    """Normalize width/case/invisible characters without losing source spans."""
    result, spans = [], []
    index = 0
    while index < len(text):
        end = index + 1
        while end < len(text) and unicodedata.combining(text[end]):
            end += 1
        cluster = unicodedata.normalize('NFKC', text[index:end]).casefold()
        for char in cluster:
            if _INVISIBLE.fullmatch(char) or unicodedata.category(char) == 'Cf':
                continue
            if char.isspace():
                if result and result[-1] != ' ':
                    result.append(' ')
                    spans.append((index, end))
                elif spans and result[-1] == ' ':
                    spans[-1] = (spans[-1][0], end)
            else:
                result.append(char)
                spans.append((index, end))
        index = end
    if result and result[-1] == ' ':
        result.pop()
        spans.pop()
    return ''.join(result), spans


def _normalize(text):
    return _normalize_with_spans(text)[0]


def _safe_line(value, limit, field):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'关键词规则的 {field} 长度或格式不正确')
    if any(unicodedata.category(char) in ('Cc', 'Cf', 'Cs') for char in value):
        raise ValueError(f'关键词规则的 {field} 不能包含换行或不可见控制字符')
    if _SECRET.search(value):
        raise ValueError(f'关键词规则的 {field} 不能包含密钥或访问令牌')
    return value.strip()


def validate(value=None):
    """Return a detached config; reject malformed or unbounded literal rules."""
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError('天网关键词设置格式不正确')
    clean = {key: copy.deepcopy(value.get(key, default)) for key, default in DEFAULT.items()}
    for key in ('enabled', 'record_warning_count'):
        if type(clean[key]) is not bool:
            raise ValueError('天网关键词开关格式不正确')
    for key, low, high in (('user_cooldown_seconds', 0, 3600), ('max_text_chars', 64, 32000)):
        if type(clean[key]) is not int or not low <= clean[key] <= high:
            raise ValueError(f'{key} 超出可设置范围')
    if not isinstance(clean['rules'], list) or len(clean['rules']) > MAX_RULES:
        raise ValueError('关键词规则最多可以保存 100 条')
    rules, seen = [], set()
    fields = {'id', 'enabled', 'category', 'phrases', 'mode', 'label', 'warning_text'}
    for raw in clean['rules']:
        if not isinstance(raw, dict) or set(raw) != fields:
            raise ValueError('关键词规则字段不完整或包含未知字段')
        ident = raw['id']
        if not isinstance(ident, str) or not _RULE_ID.fullmatch(ident) or ident in seen:
            raise ValueError('关键词规则编号应为唯一的字母、数字或下划线编号')
        if type(raw['enabled']) is not bool:
            raise ValueError('关键词规则开关格式不正确')
        if raw['category'] not in CATEGORIES or raw['mode'] not in ('contains', 'exact'):
            raise ValueError('关键词规则类型或匹配方式不正确')
        phrases = raw['phrases']
        if not isinstance(phrases, list) or not 1 <= len(phrases) <= MAX_PHRASES_PER_RULE:
            raise ValueError('每条关键词规则需要 1 至 20 个字面短语')
        normalized, final_phrases = set(), []
        for phrase in phrases:
            phrase = _safe_line(phrase, 80, '短语')
            canonical = _normalize(phrase)
            if not canonical or canonical in normalized:
                raise ValueError('关键词短语不能为空或重复')
            normalized.add(canonical)
            final_phrases.append(phrase)
        warning = _safe_line(raw['warning_text'], 200, '警告文案')
        if '[cq:' in warning.casefold():
            raise ValueError('警告文案只能使用普通文字，不能加入 QQ 控制码')
        rules.append({'id': ident, 'enabled': raw['enabled'], 'category': raw['category'],
                      'phrases': final_phrases, 'mode': raw['mode'],
                      'label': _safe_line(raw['label'], 80, '名称'), 'warning_text': warning})
        seen.add(ident)
    clean['rules'] = rules
    return clean


def _sender(value):
    if type(value) is int and 0 < value <= 2 ** 64:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 20:
        number = int(value)
        if 0 < number <= 2 ** 64:
            return str(number)
    return None


def _identifier(value):
    if type(value) is int and value.bit_length() <= 128:
        return str(value)
    if isinstance(value, str) and 0 < len(value) <= 128 and not any(
            unicodedata.category(char) in ('Cc', 'Cf', 'Cs') for char in value):
        return value
    return None


_CONTEXT_MARKERS = (
    '引用', '转述', '转发', '原话', '他说', '她说', '有人说', '那人说', '台词', '角色说',
    '小说', '剧本', '电影', '电视剧', '故事里', '游戏里', '游戏中', '反派', '虚构',
    '关键词', '词库', '案例', '例句', '举例', '例如', '比如', '讨论', '教学', '科普',
    '不要说', '不能说', '别说', '不该说', '不应该说', '不会说', '不是说', '不是要',
    '不会杀', '不想杀', '不要杀', '不能杀', '别杀', '不许杀', '没想杀', '没有要',
    '开玩笑', '闹着玩', '逗你', '玩笑话', '玩梗', '才怪', '说着玩',
    '别当真', '不要当真', '别真当', '不要真当', '当作威胁', '当成威胁',
    '哈哈', '哈哈哈', '嘿嘿', '笑死', '互相打趣', '你同意', '对方同意', '自愿',
    '[cq:reply', '[cq:forward', 'quote', 'quoted', 'fiction', 'joking', 'just kidding',
    'example', 'he said', 'she said', 'they said', 'do not say', "don't say",
)
_QUOTATION_MARKS = '“”‘’「」『』《》«»\"\'`'


def _context_reasons(texts):
    reasons = []
    context = '\n'.join(texts)
    normalized = _normalize(context).translate(_CONTEXT_SIMPLIFIED)
    if any(mark in context or mark in normalized for mark in _QUOTATION_MARKS):
        reasons.append('quotation')
    if any(marker in normalized for marker in _CONTEXT_MARKERS):
        reasons.append('context_marker')
    # A conservative exception gate, not an ideology/sexual-content detector.
    if re.search(r'(?:不|没|别|勿|莫|不能|不要|不会|不许|并非|否认).{0,12}(?:杀|弄死|捅|砍|强奸|轮奸|辱骂|傻逼|操|草|畜生|自杀)', normalized):
        reasons.append('negation')
    return reasons


def match(item, config=None):
    """Match originals separately; ambiguous context returns review, never warn.

    Sources are item['segments'] when present, otherwise [item]. A phrase cannot
    cross a source-message boundary. The verdict is only a candidate for the
    caller's protected-user/role/delivery checks and does not itself act.
    """
    none = {'disposition': 'none', 'matches': [], 'reason': 'keyword_no_match', 'verdict': None}
    # Callers persist configuration through validate(); disabled processing does
    # not need to copy the complete default library for every collected turn.
    if isinstance(config, dict) and config.get('enabled') is False:
        return {**none, 'reason': 'keyword_disabled'}
    config = validate(config)
    if not isinstance(item, dict):
        return none
    raw_sources = item.get('segments')
    if not isinstance(raw_sources, list) or not raw_sources:
        raw_sources = [item]
    bounded = (len(raw_sources) > MAX_SOURCES or item.get('keyword_context_truncated') is True
               or item.get('truncated') is True or item.get('text_truncated') is True)
    sender = _sender(item.get('user_id'))
    sources, source_ids = [], []
    remaining_chars = config['max_text_chars']
    for index, raw in enumerate(raw_sources[:MAX_SOURCES]):
        if not isinstance(raw, dict) or not isinstance(raw.get('text'), str):
            bounded = True
            continue
        text = raw['text']
        bounded = (bounded or len(text) > remaining_chars
                   or raw.get('text_truncated') is True or raw.get('truncated') is True)
        text = text[:remaining_chars]
        remaining_chars -= len(text)
        actor = _sender(raw.get('user_id', item.get('user_id')))
        ident = _identifier(raw.get('message_id', raw.get('source_id', raw.get('id'))))
        normalized, spans = _normalize_with_spans(text)
        sources.append({'source_index': index, 'source_id': ident, 'actor': actor,
                        'text': text, 'normalized': normalized, 'spans': spans})
        source_ids.append(ident)
    if not sources:
        return none
    matches = []
    for rule in config['rules']:
        if not rule['enabled']:
            continue
        evidence = []
        for source in sources:
            for phrase in rule['phrases']:
                needle = _normalize(phrase)
                haystack = source['normalized']
                offset = haystack.find(needle) if rule['mode'] == 'contains' else (0 if haystack == needle else -1)
                if offset < 0:
                    continue
                start = source['spans'][offset][0]
                end = source['spans'][offset + len(needle) - 1][1]
                evidence.append({'source_index': source['source_index'], 'source_id': source['source_id'],
                                 'quote': source['text'][start:end], 'start': start, 'end': end,
                                 'phrase': phrase})
                break
        if evidence:
            matches.append({'rule_id': rule['id'], 'label': rule['label'], 'category': rule['category'],
                            'warning_text': rule['warning_text'], 'evidence': evidence})
    if not matches:
        return none
    ambiguous = _context_reasons([source['text'] for source in sources])
    if item.get('quoted') is True or item.get('forwarded') is True or any(
            raw.get('quoted') is True or raw.get('forwarded') is True
            for raw in raw_sources[:MAX_SOURCES] if isinstance(raw, dict)):
        ambiguous.append('source_quote_or_forward')
    if bounded:
        ambiguous.append('bounded_context')
    if sender is None or any(source['actor'] is None or source['actor'] != sender for source in sources):
        ambiguous.append('unknown_or_mixed_sender')
    if any(ident is None for ident in source_ids) or len(set(source_ids)) != len(source_ids):
        ambiguous.append('unknown_or_duplicate_source')
    if ambiguous:
        return {'disposition': 'review', 'matches': matches, 'reason': 'keyword_context_review',
                'context_reasons': list(dict.fromkeys(ambiguous)), 'verdict': None}
    # The strongest matched category determines one warning, not one warning per word.
    priority = {'threat': 0, 'explicit_sexual': 1, 'targeted_abuse': 2}
    chosen = min(matches, key=lambda candidate: priority[candidate['category']])
    verdict = {'rule_id': chosen['rule_id'], 'category': chosen['category'], 'confidence': 1.0,
               'direct_violation': True, 'keyword_only': True, 'autoeligible': True,
               'warning_text': chosen['warning_text'], 'evidence': copy.deepcopy(chosen['evidence'])}
    return {'disposition': 'warn', 'matches': matches, 'reason': 'keyword_clear_match', 'verdict': verdict}
