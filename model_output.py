"""Validate public model output without leaking provider bodies into logs."""
import json


class OutputError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def content(response):
    if not isinstance(response, dict) or not isinstance(response.get('choices'), list) or not response['choices']:
        raise OutputError('ModelMissingChoices')
    choice = response['choices'][0]
    if not isinstance(choice, dict):
        raise OutputError('ModelMissingChoices')
    if choice.get('finish_reason') == 'length':
        raise OutputError('ModelOutputTruncated')
    message = choice.get('message')
    value = message.get('content') if isinstance(message, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise OutputError('EmptyReply')
    return value


def decision(response):
    try:
        result = json.loads(content(response))
    except json.JSONDecodeError:
        raise OutputError('ModelInvalidJSON') from None
    if not isinstance(result, dict) or set(result) - {'speak', 'messages', 'sticker_id'} or type(result.get('speak')) is not bool:
        raise OutputError('ModelInvalidSchema')
    if result['speak']:
        messages = result.get('messages', [])
        if not isinstance(messages, (str, list)) or isinstance(messages, list) and any(not isinstance(row, str) for row in messages):
            raise OutputError('ModelInvalidSchema')
        if result.get('sticker_id') is not None and not isinstance(result.get('sticker_id'), str):
            raise OutputError('ModelInvalidSchema')
    return result
