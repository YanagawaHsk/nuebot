import copy,json,os,time,re
import plugin_features
import memory_learning
import ai_guard
import model_gate
import moderation_intake
from local_identity import BOT_ID,OWNER_ID,DEFAULT_GROUP
from pathlib import Path
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parent
PATH=ROOT/'settings.json'
RANGES={'collect_quiet':(5,60),'collect_incomplete':(5,90),'collect_max':(15,120),'reply_ttl':(30,180),'topic_gap':(60,900),'partition_gap':(30,900),'partition_span':(60,1800),'reference_age':(30,1800),'retry_attempts':(1,5),'retry_base':(3,60),'context_age':(60,1800),'context_messages':(1,40),'output_tokens':(32,4096),'messages_hour':(1,120),'model_calls_hour':(1,360),'cooldown_seconds':(0,3600),'delay_min':(0,60),'delay_max':(0,90),'mention_probability':(0,1),'topic_interval':(300,86400),'sticker_hour':(0,30),'sticker_interval':(0,3600)}
DEFAULT_RUNTIME={'collect_quiet':12,'collect_incomplete':20,'collect_max':45,'reply_ttl':120,'topic_gap':120,'context_age':300,'time_partition_enabled':True,'partition_gap':90,'partition_span':300,'reference_age':180,'auto_retry':True,'retry_attempts':3,'retry_base':5,'context_messages':15,'output_tokens':128,'messages_hour':30,'model_calls_hour':120,'cooldown_seconds':120,'delay_min':8,'delay_max':12,'mention_probability':.9,'topic_enabled':True,'topic_interval':3600,'stickers_enabled':True,'sticker_hour':3,'sticker_interval':600,'challenge_filter':True,'catchphrase_filter':True,'chat_enabled':True,'mention_only':False}
DEFAULT_RELATIONSHIPS=[]

def connection_defaults():
    model=json.loads((ROOT/'model.json').read_text(encoding='utf-8'))
    return {'group_id':DEFAULT_GROUP,'base_url':model['base_url'],'model':model['model'],'disable_thinking':True}

def validate_connection(value):
    group=value.get('group_id');url=value.get('base_url','').strip().rstrip('/');model=value.get('model','').strip()
    if type(group) is not int or not 10000<=group<=999999999999:raise ValueError('群号格式不正确')
    parsed=urlsplit(url)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:raise ValueError('请填写API基础地址，不含密钥或查询参数')
    if parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in ('127.0.0.1','localhost','::1')):raise ValueError('API地址须使用HTTPS；本机服务可使用HTTP')
    if not model or len(model)>150 or any(c in model for c in '\r\n'):raise ValueError('模型名称格式不正确')
    if type(value.get('disable_thinking')) is not bool:raise ValueError('模型协议开关不正确')
    return {'group_id':group,'base_url':url,'model':model,'disable_thinking':value['disable_thinking']}

def defaults():
    return {'security':dict(ai_guard.DEFAULT),'version':1,'plugins':plugin_features.validate({}),'relationships':copy.deepcopy(DEFAULT_RELATIONSHIPS),'connection':connection_defaults(),'persona':(ROOT/'persona.txt').read_text(encoding='utf-8'),'runtime':copy.deepcopy(DEFAULT_RUNTIME),'moderation':json.loads((ROOT/'moderation.json').read_text(encoding='utf-8')),'keywords':[]}

def validate_runtime(value):
    if not isinstance(value,dict):raise ValueError('群聊天设置格式不正确')
    runtime={**DEFAULT_RUNTIME,**value}
    for key,(low,high) in RANGES.items():
        v=runtime.get(key)
        if type(v) not in (int,float) or not low<=v<=high or (key!='mention_probability' and type(v) is not int):raise ValueError(f'{key} 超出可设置范围')
    if runtime['collect_incomplete']<runtime['collect_quiet'] or runtime['collect_max']<runtime['collect_incomplete'] or runtime['reply_ttl']<=runtime['collect_max']:raise ValueError('等待时长应满足：停顿 ≤ 未完句 ≤ 长段收集 < 回复有效期')
    if runtime['delay_max']<runtime['delay_min']:raise ValueError('最长等待不能小于最短等待')
    for key in ('auto_retry','topic_enabled','stickers_enabled','challenge_filter','catchphrase_filter','chat_enabled','mention_only','time_partition_enabled'):
        if type(runtime.get(key)) is not bool:raise ValueError('开关设置不正确')
    if runtime['time_partition_enabled'] and runtime['partition_gap']<=runtime['collect_incomplete']:raise ValueError('启用时间分区时，新分区空闲间隔必须长于未完句等待')
    return {k:runtime[k] for k in DEFAULT_RUNTIME}

def runtime_for(value,group):
    return copy.deepcopy(value.get('runtime_groups',{}).get(str(group),value['runtime']))

def validate_runtime_groups(rows,runtime,gids):
    if not isinstance(rows,dict) or len(rows)>50:raise ValueError('最多保存50个群的聊天设置')
    clean={}
    for gid,profile in rows.items():
        if not str(gid).isdigit() or not 10000<=int(gid)<=999999999999:raise ValueError('聊天设置群号不正确')
        clean[str(int(gid))]=validate_runtime(profile)
    for gid in gids:clean.setdefault(str(gid),copy.deepcopy(runtime))
    if len(clean)>50:raise ValueError('最多保存50个群的聊天设置')
    return clean

def validate_account_limits(value,runtime):
    if value is None:value={'enabled':True,'messages_hour':runtime['messages_hour'],'model_calls_hour':runtime['model_calls_hour']}
    if not isinstance(value,dict) or type(value.get('enabled')) is not bool:raise ValueError('总额度保护开关不正确')
    for key,maximum in (('messages_hour',2400),('model_calls_hour',7200)):
        if type(value.get(key)) is not int or not 1<=value[key]<=maximum:raise ValueError('所有群总额度超出范围')
    return {k:value[k] for k in ('enabled','messages_hour','model_calls_hour')}

def validate(value):
    if not isinstance(value,dict):raise ValueError('配置格式不正确')
    connection=validate_connection(value.get('connection') or connection_defaults())
    relationships=validate_relationships(value.get('relationships',copy.deepcopy(DEFAULT_RELATIONSHIPS)))
    persona=value.get('persona')
    if not isinstance(persona,str) or not 1<=len(persona)<=30000:raise ValueError('人设需要1–30000个字符')
    if 'sk-' in persona:raise ValueError('请勿把API密钥写入人设')
    runtime=validate_runtime(value.get('runtime',{}))
    policy=value.get('moderation',{})
    for key in ('enabled','warnings_enabled','punishments_enabled'):
        if type(policy.get(key)) is not bool:raise ValueError('天网开关设置不正确')
    for key,lo,hi in [('warnings_before_mute',1,5),('warning_window_seconds',60,7200),('individual_mute_seconds',60,3600)]:
        if type(policy.get(key)) is not int or not lo<=policy[key]<=hi:raise ValueError('天网次数或时长超出范围')
    if type(policy.get('minimum_confidence')) not in (int,float) or not .9<=policy['minimum_confidence']<=1:raise ValueError('判定置信度范围为0.9–1')
    fixed=json.loads((ROOT/'moderation.json').read_text(encoding='utf-8'))
    fixed.update({k:policy[k] for k in ('enabled','warnings_enabled','punishments_enabled','warnings_before_mute','warning_window_seconds','individual_mute_seconds','minimum_confidence')})
    fixed['group_id']=connection['group_id']
    rules=value.get('keywords',[])
    if not isinstance(rules,list) or len(rules)>50:raise ValueError('最多50条关键词规则')
    clean=[]
    for rule in rules:
        if not isinstance(rule,dict) or rule.get('mode') not in ('exact','contains'):raise ValueError('关键词仅支持完全匹配或包含匹配')
        words=rule.get('keywords',[]);response=rule.get('response','');uid=str(rule.get('user_id','')).strip()
        if not isinstance(words,list) or not 1<=len(words)<=20 or any(not isinstance(w,str) or not 1<=len(w)<=100 for w in words):raise ValueError('每条规则需要1–20个关键词')
        if not isinstance(response,str) or not 1<=len(response)<=140 or '[CQ:' in response or 'sk-' in response:raise ValueError('固定回应需1–140字，不可含CQ指令或密钥')
        if uid and (not uid.isdigit() or not 10000<=int(uid)<=99999999999):raise ValueError('限定QQ号格式不正确')
        if type(rule.get('enabled')) is not bool or type(rule.get('require_mention')) is not bool:raise ValueError('规则开关不正确')
        clean.append({'enabled':rule['enabled'],'name':str(rule.get('name','新规则'))[:40],'mode':rule['mode'],'keywords':words,'response':response,'user_id':uid,'require_mention':rule['require_mention']})
    subscriptions=validate_subscriptions(value.get('groups'),connection['group_id'])
    groups=validate_plugin_groups(value.get('plugin_groups'),value.get('plugins',{}),connection['group_id'])
    learning=memory_learning.validate_groups(value.get('learning_groups',{}))
    runtime_groups=validate_runtime_groups(value.get('runtime_groups',{}),runtime,{row['group_id'] for row in subscriptions}|set(groups)|set(learning)|{connection['group_id']})
    return {'moderation_intake':moderation_intake.validate(value.get('moderation_intake')),'model_control':model_gate.validate(value.get('model_control',{})),'runtime_groups':runtime_groups,'account_limits':validate_account_limits(value.get('account_limits'),runtime),'security':ai_guard.validate(value.get('security',{})),'version':2,'learning_groups':learning,'groups':subscriptions,'plugin_groups':groups,'plugins':groups.get(str(connection['group_id']),plugin_features.validate({})),'relationships':relationships,'connection':connection,'persona':persona,'runtime':{k:runtime[k] for k in DEFAULT_RUNTIME},'moderation':fixed,'keywords':clean}

def validate_subscriptions(rows,primary):
    if rows is None:rows=[{'group_id':primary,'enabled':True}]
    if not isinstance(rows,list) or len(rows)>20:raise ValueError('最多同时配置20个群')
    clean=[];seen=set()
    for row in rows:
        gid=row.get('group_id') if isinstance(row,dict) else None
        if type(gid) is not int or not 10000<=gid<=999999999999 or gid in seen or type(row.get('enabled')) is not bool:raise ValueError('参与群设置不正确或重复')
        seen.add(gid);clean.append({'group_id':gid,'enabled':row['enabled']})
    return clean

def validate_plugin_groups(rows,legacy,group):
    if rows is None:rows={str(group):legacy}
    if not isinstance(rows,dict) or len(rows)>50:raise ValueError('最多保存50个群的插件配置')
    clean={}
    for gid,config in rows.items():
        if not str(gid).isdigit() or not 10000<=int(gid)<=999999999999:raise ValueError('插件群号不正确')
        clean[str(int(gid))]=plugin_features.validate(config)
    return clean

def validate_relationships(rows):
    if not isinstance(rows,list) or len(rows)>50:raise ValueError('特殊对待最多50位群友')
    clean=[];seen=set()
    for row in rows:
        if not isinstance(row,dict):raise ValueError('特殊对待表格格式不正确')
        uid=str(row.get('user_id','')).strip()
        if not uid.isdigit() or not 10000<=int(uid)<=99999999999:raise ValueError('特殊对待需要有效QQ号')
        uid=str(int(uid))
        if uid in seen:raise ValueError('同一QQ号只能设置一条特殊对待关系')
        seen.add(uid)
        if type(row.get('enabled')) is not bool:raise ValueError('关系启用开关不正确')
        entry={'enabled':row['enabled'],'user_id':uid}
        for key,limit in [('name',40),('relationship',80),('address',40),('behavior',600)]:
            v=row.get(key,'')
            if not isinstance(v,str) or len(v)>limit or 'sk-' in v:raise ValueError('关系字段过长或含有密钥')
            entry[key]=v.strip()
        if not entry['name'] or not entry['relationship']:raise ValueError('请填写名字与关系')
        clean.append(entry)
    return clean

def relationship_for(rows,user_id):
    return next(({k:v for k,v in row.items() if k not in ('user_id','enabled')} for row in rows if row['enabled'] and str(user_id)==row['user_id']),None)

def load():
    return validate(json.loads(PATH.read_text(encoding='utf-8'))) if PATH.exists() else validate(defaults())

def save(value):
    value=validate(value)
    if PATH.exists():
        backup=ROOT/'settings-backups';backup.mkdir(exist_ok=True)
        (backup/f'{time.time_ns()}.json').write_bytes(PATH.read_bytes())
        for p in sorted(backup.glob('*.json'))[:-10]:p.unlink()
    tmp=PATH.with_suffix('.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(tmp,PATH)
    return value

def match_keyword(rules,user_id,text,mentioned=False):
    plain=text.replace('@鵺','').strip().rstrip('！!~～。呀吧啊嘛')
    for rule in rules:
        if not rule['enabled'] or (rule['user_id'] and str(user_id)!=rule['user_id']) or (rule['require_mention'] and not mentioned):continue
        hit=any(plain==w if rule['mode']=='exact' else w in plain for w in rule['keywords'])
        if hit and rule['user_id'] and rule['response'] in ('🐔','鸡') and re.search(r'(?:别|不要|不许|不准|停止|不想|不能|莫)[^，。！？!?]{0,8}(?:欺负我|打我)|(?:受伤|真的疼|求救)|(?:他说|她说|别人说)[^，。！？!?]*(?:欺负我|打我)|^(?:你|他|她|他们|别人)(?:又|在|正在|一直|总是)?(?:欺负我|打我)',plain):continue
        if hit:return rule['response']
    return None
