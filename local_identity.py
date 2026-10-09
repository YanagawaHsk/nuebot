"""Private machine identity: account.json is never part of a release."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parent
try:ACCOUNT=json.loads((ROOT/'account.json').read_text(encoding='utf-8'))
except FileNotFoundError:raise RuntimeError('请先运行 setup_local.py，填写本机账号配置') from None
def number(key):
    value=ACCOUNT[key]
    if type(value) is not int or not 10000<=value<=999999999999:raise ValueError('本机账号配置不正确：'+key)
    return value
BOT_ID,OWNER_ID,DEFAULT_GROUP=number('bot_id'),number('owner_id'),number('default_group')
def onebot_path():
    return Path(ACCOUNT['onebot_config']).expanduser() if ACCOUNT.get('onebot_config') else ROOT.parent/'SnowLuma-v1.14.19'/'config'/f'onebot_{BOT_ID}.json'
