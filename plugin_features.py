"""Native adapters for imported NcatBot features; never imports supplied scripts."""
import hashlib,json,random,re,time,unicodedata,urllib.request,urllib.parse,threading
import plugin_manager
import tempfile
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parent
ASSETS=ROOT/'plugin-assets'
TZ=timezone(timedelta(hours=8))

@contextmanager
def _state_guard(path):
    """Serialize receipt settlement across worker and panel processes."""
    import msvcrt
    guard=path.with_suffix(path.suffix+'.lock')
    with guard.open('a+b') as handle:
        # Windows permits byte-range locks past EOF. Initializing byte zero
        # before locking races another process that already owns that byte.
        end=time.monotonic()+5
        while True:
            try:handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1);break
            except OSError:
                if time.monotonic()>=end:raise TimeoutError('StorageError')
                time.sleep(.05)
        try:yield
        finally:handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)

CATALOG=[
 {'id':'dice','name':'骰子与COC判定','sources':['DicePlugin'],'description':'投骰与技能判定，最多10个100面骰子','commands':'.r3d6；.ra侦察75','status':'ready'},
 {'id':'jrrp','name':'今日人品','sources':['JRRPChecker'],'description':'同一群友同一天得到稳定的1–100结果，仅娱乐','commands':'jrrp；今日人品','status':'ready'},
 {'id':'fortune','name':'探女抽签','sources':['FortuneDraw'],'description':'从包内128张签图随机抽取','commands':'探女抽签；鵺抽签','status':'ready'},
 {'id':'tarot','name':'塔罗正逆位','sources':['TarotReader'],'description':'22张大阿尔卡那、正逆位及牌意，仅娱乐','commands':'探女占卜；鵺占卜；姐姐占卜','status':'ready'},
 {'id':'food','name':'今天吃什么','sources':['WhatToEat'],'description':'32种食物随机推荐，避免连续重复；图片保留署名','commands':'今天吃什么','status':'ready'},
 {'id':'music','name':'东方原曲抽取','sources':['TouhouOriginalMusicDraw'],'description':'包内原曲名称、作品与位置；原包没有音频或作品图片，提供文字','commands':'今日原曲；抽原曲；随机原曲','status':'ready'},
 {'id':'spell','name':'东方符卡抽取','sources':['TouhouSpellCardDraw'],'description':'120条符卡、使用者与作品，纯文字模式','commands':'今日符卡；抽符卡','status':'ready'},
 {'id':'hourly','name':'整点报时','sources':['HourlyNotifier'],'description':'北京时间整点报时，可选活跃时段，只在当前目标群；冷却或额度不足则跳过','commands':'自动整点触发','status':'ready'},
 {'id':'help','name':'功能帮助','sources':['HelpPlugin'],'description':'按实际开关列出已启用指令，不沿用原作者账号','commands':'help；鵺帮助；插件帮助','status':'ready'},
 {'id':'say','name':'角色招呼','sources':['Myplugin'],'description':'保留“探女说话”彩蛋，新增“鵺说话”','commands':'探女说话；鵺说话','status':'ready'},
 {'id':'image_reply','name':'图片后接话','sources':['Reply'],'description':'目标群有人发图后，下一条消息回复一次u；不识别图片内容','commands':'发图后的下一条消息','status':'ready'},
 {'id':'jm_joke','name':'JM编号玩笑','sources':['Jmsearcher'],'description':'jm加1–7位数字时回一句“休息一下吧”；原代码不搜索或下载漫画','commands':'jm123456','status':'ready'},
 {'id':'bilibili','name':'B站视频信息','sources':['BilibiliUrlParser'],'description':'识别BV、av号，读取标题、UP主与链接；不接入原版视频下载、Cookie及短链接跳转','commands':'发送BV号或av号、含编号的B站链接','status':'ready','network':'仅视频编号发送到api.bilibili.com，不传其他群聊文字'},
 {'id':'anime','name':'全年龄二次元取图','sources':['AnimeImageFetcher'],'description':'r18=0、排除AI、标题标签过滤；每人每天限额，成功发送才计数。元数据过滤不能保证图片内容','commands':'来张二次元 [标签]；来张美少女；图片额度','status':'ready','network':'仅输入的检索标签发送到api.lolicon.app，图片来自i.pixiv.re或i.pximg.net；不传QQ号、上下文或密钥'},
 {'id':'autoreply','name':'关键词自动回复','sources':['AutoReplyPlugin'],'description':'已合并到“关键词回应”，支持完全/包含匹配、限定QQ、必须@和冷却','commands':'使用关键词回应页面','status':'merged','tab':'rules-tab'},
 {'id':'chat','name':'FreeChat与Kimi聊天系列','sources':['FreeChatLite','FreeChatLite_past','FreeChatLite_past1','FreeChatLite_past2','KimiChatLite','KimiChatLite_past'],'description':'已合并为现有API、人设、上下文、频率及表情功能。旧后台不并行启动；额外长期记忆不启用','commands':'人设、记忆与节奏、连接设置页面','status':'merged','tab':'connection-tab'},
 {'id':'forward','name':'群消息搬运','sources':['Forward','Forward_old'],'description':'原功能支持跨群路由和合并转发。目前只有一个授权目标群，缺少来源/目的群与转发范围，暂未接入','commands':'需先指定跨群转发范围','status':'unavailable'},
 {'id':'test','name':'示例测试插件','sources':['Test'],'description':'原代码给固定QQ发送测试图片/文件；已替换为面板本地试用，不发测试消息','commands':'本页本地试用','status':'merged','tab':'plugins-tab'},
]
READY={p['id'] for p in CATALOG if p['status']=='ready'}
DEFAULTS={'enabled':False,'features':{key:False for key in READY},'images_enabled':True,'daily_limit':20,'image_daily_limit':5,'hour_start':8,'hour_end':23,'texts':{}}
VARIABLES={'dice':['result','skill','target','roll'],'jrrp':['result','score'],'fortune':['result'],'tarot':['result','name','position','meaning'],'food':['result','food'],'music':['result','name','work','position'],'spell':['result','name','owner','work','position'],'hourly':['result','time'],'help':['result'],'say':['result'],'image_reply':['result'],'jm_joke':['result'],'bilibili':['result'],'anime':['result']}
TEXT_GUIDE={
 'dice':('投骰：🎲 各骰点数相加 = 总点数；技能判定：🎲 技能：点数/目标值，判定结果','鵺替你掷一下：{result}'),
 'jrrp':('今日人品 {score}/100，妖怪随手测的，图个乐就好','今天的地球运气是 {score}/100'),
 'fortune':('探女的签箱借我一下，给你抽这一张','签箱给你翻一张：{result}'),
 'tarot':('🔮 {name} · {position}：{meaning}，只是抽牌小游戏哦','抽到了 {name}（{position}）：{meaning}'),
 'food':('今天就吃{food}，挑挑看？','鵺替你选了 {food}，怎么样？'),
 'music':('🎵 曲名（原文曲名）· 作品 · 曲目位置 · 作曲者','今天听 {name}，来自 {work}'),
 'spell':('🎴 {name} · {owner} · {work} {position}','这次抽到 {owner} 的 {name}，出自 {work}'),
 'hourly':('⏰ {time}了，地球时间又走过一圈','{time} 了，鵺看看你们'),
 'help':('现在能用：已启用的插件指令，以分号分隔','想玩的话，现在有这些：{result}'),
 'say':('鵺说话：鵺在呢，今天想聊什么？；探女说话：我是佐具卖，仅凭言语便能改变世界的走向','鵺在这里，有什么可爱的事情？'),
 'image_reply':('u','好可爱，给我看看'),
 'jm_joke':('先休息一下吧，眼睛也要放个假','先让眼睛休息一下吧'),
 'bilibili':('视频标题 · UP 作者名 · B站视频链接','这个视频：{result}'),
 'anime':('图片标题 · 作者 作者名 · Pixiv作品链接；查询额度和帮助时返回对应说明','鵺挑到这张：{result}'),
}

def panel_catalog():
    hidden=plugin_manager.registry().get('hidden_entries',[])
    result=[]
    for p in CATALOG:
        if p['id'] in hidden or (p['id']=='jm_joke' and not plugin_manager.installed('jm_joke')):continue
        default,example=TEXT_GUIDE.get(p['id'],('',''))
        result.append({**p,'installed':plugin_manager.installed(p['id']),'variables':VARIABLES.get(p['id'],[]),'default_text':default,'template_example':example})
    return result

def validate(value):
    if not isinstance(value,dict):raise ValueError('插件设置格式不正确')
    result={**DEFAULTS,**value};flags=value.get('features',{})
    if not isinstance(flags,dict):raise ValueError('插件开关格式不正确')
    result['features']={key:flags.get(key,False) for key in READY}
    if any(type(v) is not bool for v in result['features'].values()) or type(result['enabled']) is not bool or type(result['images_enabled']) is not bool:raise ValueError('插件开关必须为开或关')
    for key,lo,hi in [('daily_limit',1,100),('image_daily_limit',1,20),('hour_start',0,23),('hour_end',1,24)]:
        if type(result[key]) is not int or not lo<=result[key]<=hi:raise ValueError('插件限额或时段格式不正确')
    if result['hour_start']>=result['hour_end']:raise ValueError('报时结束时间应晚于开始时间')
    texts=value.get('texts',{})
    if not isinstance(texts,dict) or any(key not in READY for key in texts):raise ValueError('插件文案格式不正确')
    cleaned={}
    for key,text in texts.items():
        if not isinstance(text,str) or len(text)>420 or 'sk-' in text or '[CQ:' in text or '\n' in text or '\r' in text:raise ValueError('文案最多420字，不含换行、密钥或CQ指令')
        tokens=re.findall(r'\{([^{}]*)\}',text)
        if any(token not in VARIABLES[key] for token in tokens) or re.search(r'[{}]',re.sub(r'\{[^{}]*\}','',text)):raise ValueError('文案中有不支持的变量')
        if text.strip():cleaned[key]=text.strip()
    result['texts']=cleaned
    return {k:result[k] for k in DEFAULTS}

def enabled(config,key):return config['enabled'] and config['features'].get(key,False) and plugin_manager.installed(key)
def clean(value,limit=140):
    text=re.sub(r'\s+',' ',str(value)).strip()
    if 'sk-' in text or '[CQ:' in text:raise ValueError('Unsafe plugin text')
    return text[:limit]
def read(name):return json.loads((ASSETS/name).read_text(encoding='utf-8'))
def optional_read(name,default):
    try:return read(name)
    except FileNotFoundError:return default

class Engine:
    def __init__(self,state_path=None):
        self.path=state_path or ROOT/'plugin-state.json';self.lock=threading.Lock();self.image_wait={};self.hour_seen={}
        try:self.state=json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:self.state={'usage':{},'last':{}}
        self.refresh_assets()
        self.food_credit={}
        credit_file=ASSETS/'WhatToEat/IMAGE_SOURCES.md'
        for line in credit_file.read_text(encoding='utf-8').splitlines() if credit_file.exists() else []:
            fields=[f.strip() for f in line.split('|')]
            if len(fields)>=7 and fields[1].startswith('`'):self.food_credit[fields[1].strip('`')]=fields[3:6]
        self.net=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    def refresh_assets(self):
        self.foods=optional_read('WhatToEat/foods.json',[]);self.music=optional_read('TouhouOriginalMusicDraw/tracks.json',[]);self.spells=optional_read('TouhouSpellCardDraw/spellcards.json',[]);self.tarot=optional_read('tarot-data.json',{'cards':{},'meanings':{}})
        self.blocked=optional_read('image-filter.json',{'blocked_terms':[]})['blocked_terms']
    def detect(self,text,event,config):
        if not config['enabled']:return None
        plain=text.replace('@鵺','').strip();lower=plain.lower();key=None;args=''
        commands={'jrrp':['jrrp','今日人品'],'fortune':['探女抽签','鵺抽签'],'tarot':['探女占卜','鵺占卜','姐姐占卜'],'food':['今天吃什么','今天吃什么？','今天吃什么?'],'help':['help','鵺帮助','插件帮助'],'say':['探女说话','鵺说话']}
        for k,words in commands.items():
            if lower in words:key=k;args=plain if k=='say' else '';break
        for k,folder in [('music','TouhouOriginalMusicDraw'),('spell','TouhouSpellCardDraw')]:
            if enabled(config,k) and lower in [w.lower() for w in optional_read(folder+'/config.json',{'triggers':[]})['triggers']]:key=k
        if re.fullmatch(r'\.r\d{1,3}d\d{1,3}|\.ra[^\r\n]{1,30}\d{1,3}',lower):key='dice';args=plain
        if re.fullmatch(r'jm\d{1,7}',lower):key='jm_joke'
        video=re.search(r'(?<![A-Za-z0-9])(BV[1-9A-HJ-NP-Za-km-z]{10}|av\d{1,12})(?![A-Za-z0-9])',plain,re.I)
        if video:key='bilibili';args=video.group(1)
        if lower in ('图片额度','今日图片额度','二次元帮助','图片帮助'):key='anime';args=lower
        for prefix in ('来张二次元','来张美少女','随机二次元','二次元图片'):
            if plain==prefix or plain.startswith(prefix+' '):key='anime';args=plain[len(prefix):].strip()
        group=event.get('group_id')
        if enabled(config,'image_reply'):
            if any(p.get('type')=='image' for p in event.get('message',[])):
                self.image_wait[group]=time.time()+60
            elif self.image_wait.pop(group,0)>time.time() and not key:key='image_reply'
        if key and enabled(config,key):return {'id':key,'args':args,'user_id':event.get('user_id'),'group_id':group}
        return None
    def hourly(self,group,config):
        now=datetime.now(TZ);slot=now.strftime('%Y-%m-%d %H')
        if not enabled(config,'hourly') or now.minute!=0 or not config['hour_start']<=now.hour<config['hour_end'] or self.hour_seen.get(group)==slot or self.state['last'].get(str(group)+':hourly')==slot:return None
        self.hour_seen[group]=slot
        return {'id':'hourly','args':now.strftime('%H:00'),'user_id':0,'group_id':group,'hour_slot':slot}
    def save(self):
        today=datetime.now(TZ).strftime('%Y-%m-%d')
        self.state['usage']={k:v for k,v in self.state['usage'].items() if k.startswith(today)}
        import shared_budget
        temporary=None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent,prefix='plugin-state-',suffix='.tmp',delete=False) as stream:
                temporary=Path(stream.name);stream.write(json.dumps(self.state).encode('utf-8'))
            shared_budget.replace_with_retry(temporary,self.path)
        finally:
            if temporary is not None and temporary.exists():temporary.unlink()
    def confirmed(self,result,delivery_id=None):
        if not result.get('quota_key'):return
        with self.lock,_state_guard(self.path):
            # Quota and the receipt key are committed together. Re-reading also
            # picks up an administrator's reconciliation before a later send.
            if delivery_id:
                try:self.state=json.loads(self.path.read_text(encoding='utf-8'))
                except FileNotFoundError:self.state={'usage':{},'last':{}}
                settled=self.state.setdefault('settled_deliveries',{})
                if delivery_id in settled:return False
                settled[delivery_id]=time.time()
                self.state['settled_deliveries']={k:v for k,v in settled.items() if v>time.time()-7*86400}
            key=result['quota_key'];self.state['usage'][key]=self.state['usage'].get(key,0)+1
            if result.get('selected'):self.state['last'][result['last_key']]=result['selected']
            self.save()
            return True
    def choose(self,items,group,key):
        lastkey=str(group)+':'+key;previous=self.state['last'].get(lastkey)
        candidates=[item for item in items if str(item.get('id',item.get('name','')))!=previous] or items
        item=random.choice(candidates);return item,lastkey,str(item.get('id',item.get('name','')))
    def execute(self,command,config,preview=False):
        key=command['id'];args=command.get('args','');group=command.get('group_id');uid=command.get('user_id');day=datetime.now(TZ).strftime('%Y-%m-%d')
        if not enabled(config,key):return None
        self.refresh_assets()
        qkey=f'{day}:{group}:{uid}:{key}';limit=config['image_daily_limit'] if key=='anime' else config['daily_limit']
        if not preview and key!='hourly' and self.state['usage'].get(qkey,0)>=limit:return {'id':key,'text':'今天这个功能先歇歇，额度已经用完了'}
        result={'id':key,'quota_key':qkey};text='';image=None
        if key=='dice':
            roll=re.fullmatch(r'\.r(\d{1,3})d(\d{1,3})',args,re.I)
            if roll:
                n,sides=map(int,roll.groups())
                if not 1<=n<=10 or not 1<=sides<=100:text='最多10个骰子，每个1–100面'
                else:
                    values=[random.randint(1,sides) for _ in range(n)];text='🎲 '+ ' + '.join(map(str,values))+' = '+str(sum(values))
            else:
                match=re.fullmatch(r'\.ra(.+?)(\d{1,3})',args,re.I);skill,target=match.groups();target=int(target)
                if not 1<=target<=99:text='技能目标值应在1–99之间'
                else:
                    r=random.randint(1,100);label='大成功' if r==1 else '大失败' if (r==100 or target<50 and r>=96) else '极难成功' if r<=target/5 else '困难成功' if r<=target/2 else '成功' if r<=target else '失败'
                    text=f'🎲 {skill}：{r}/{target}，{label}'
        elif key=='jrrp':
            score=int(hashlib.sha256(f'nue:{day}:{group}:{uid}'.encode()).hexdigest()[:8],16)%100+1
            text=f'今日人品 {score}/100，妖怪随手测的，图个乐就好'
        elif key=='fortune':
            image=random.choice(sorted((ASSETS/'FortuneDraw/fortunes').glob('*.jpg')));text='探女的签箱借我一下，给你抽这一张'
        elif key=='tarot':
            number=random.choice(list(self.tarot['cards']));roman,name=self.tarot['cards'][number];position=random.choice(['正位','逆位'])
            text=f'🔮 {name} · {position}：'+self.tarot['meanings'][number][position]+'，只是抽牌小游戏哦';image=ASSETS/'TarotReader/tarot'/f'{roman}.jpg'
            if position=='逆位' and config['images_enabled'] and not preview:
                try:
                    from PIL import Image
                    folder=ASSETS/'tarot-cache';folder.mkdir(exist_ok=True);flipped=folder/f'{roman}-reversed.jpg'
                    if not flipped.exists():
                        with Image.open(image) as img:img.transpose(Image.Transpose.ROTATE_180).save(flipped)
                    image=flipped
                except ImportError:pass
        elif key in ('food','music','spell'):
            item,lastkey,selected=self.choose({'food':self.foods,'music':self.music,'spell':self.spells}[key],group,key)
            result.update(last_key=lastkey,selected=selected)
            if key=='food':
                text='今天就吃'+item['name']+'，挑挑看？';image=ASSETS/'WhatToEat/images'/Path(item['image']).name
                if config['images_enabled']:
                    credit=self.food_credit.get(item['image'])
                    if credit:result['credit']='图源 '+credit[0]+' · '+credit[1]+'；已裁剪缩放；'+credit[2]
            elif key=='music':text=f"🎵 {item['name']}（{item['original_name']}）· {item['work']} · {item['position']} · 作曲 {item.get('composer','ZUN')}"
            else:text=f"🎴 {item['name']} · {item['owner']} · {item['work']} {item['stage']}"
        elif key=='help':text='现在能用：'+'；'.join(p['commands'] for p in CATALOG if enabled(config,p['id']) and p['id']!='help')
        elif key=='say':text='我是佐具卖，仅凭言语便能改变世界的走向' if args=='探女说话' else '鵺在呢，今天想聊什么？'
        elif key=='image_reply':text='u'
        elif key=='jm_joke':text='先休息一下吧，眼睛也要放个假'
        elif key=='hourly':
            text='⏰ '+args+'了，地球时间又走过一圈'
            result.update(last_key=str(group)+':hourly',selected=command.get('hour_slot'))
        elif key=='bilibili':
            if preview:text='本地预览识别到视频编号：'+args+'；预览不访问B站'
            else:
                param={'aid':args[2:]} if args.lower().startswith('av') else {'bvid':'BV'+args[2:]}
                data=self.get_json('https://api.bilibili.com/x/web-interface/view?'+urllib.parse.urlencode(param))
                if data.get('code')!=0:raise ValueError('B站信息暂不可用')
                info=data['data'];text=clean(info['title'],65)+' · UP '+clean(info.get('owner',{}).get('name','未知'),25)+' · https://www.bilibili.com/video/'+clean(info['bvid'],16)
        elif key=='anime':
            if args in ('图片额度','今日图片额度'):text='今天还可取图 '+str(max(0,limit-self.state['usage'].get(qkey,0)))+' 张';result.pop('quota_key',None)
            elif args in ('二次元帮助','图片帮助'):text='来张二次元 后面可加标签，只取全年龄图片；图片额度可查看当天剩余次数';result.pop('quota_key',None)
            elif preview:text='已识别取图指令；本地试用不访问外部接口或下载图片'
            else:
                normalized=lambda s:unicodedata.normalize('NFKC',str(s)).casefold().replace(' ','')
                if len(args)>80 or any(normalized(word) in normalized(args) for word in self.blocked if word):raise ValueError('这个标签不在全年龄取图范围')
                tags=args.split()[:3] or ['女の子']
                data=self.get_json('https://api.lolicon.app/setu/v2',{'r18':0,'num':6,'tag':tags,'size':['regular'],'excludeAI':True,'proxy':'i.pixiv.re'})
                items=[item for item in data.get('data',[]) if item.get('r18') is False and not item.get('aiType') and item.get('width',0)>=600 and item.get('height',0)>=600 and not any(normalized(word) in normalized(item.get('title','')+' '+item.get('author','')+' '+' '.join(item.get('tags',[]))) for word in self.blocked if word)]
                if not items:raise ValueError('暂时没有合适的全年龄图片')
                item=random.choice(items);url=item.get('urls',{}).get('regular','');parsed=urllib.parse.urlsplit(url)
                if parsed.scheme!='https' or parsed.hostname not in ('i.pixiv.re','i.pximg.net') or parsed.username or parsed.password:raise ValueError('图片来源不在允许范围')
                req=urllib.request.Request(url,headers={'Referer':'https://www.pixiv.net/'})
                with self.net.open(req,timeout=15) as response:
                    raw=response.read(6*1024*1024+1);content=response.headers.get('Content-Type','')
                if len(raw)>6*1024*1024 or not content.startswith('image/') or not (raw.startswith(b'\xff\xd8\xff') or raw.startswith(b'\x89PNG\r\n\x1a\n')):raise ValueError('图片格式或大小不适合发送')
                cache=ASSETS/'anime-cache';cache.mkdir(exist_ok=True)
                for old in sorted(cache.glob('*.img'),key=lambda p:p.stat().st_mtime)[:-49]:old.unlink()
                image=cache/(hashlib.sha256(raw).hexdigest()+'.img');image.write_bytes(raw)
                text=clean(item['title'],40)+' · 作者 '+clean(item.get('author',''),20)+' · https://www.pixiv.net/artworks/'+str(int(item['pid']))
        variables={'result':text,'time':args,'score':locals().get('score',''),'food':locals().get('item',{}).get('name',''),'name':locals().get('name',locals().get('item',{}).get('name','')),'work':locals().get('item',{}).get('work',''),'owner':locals().get('item',{}).get('owner',''),'position':locals().get('position',locals().get('item',{}).get('position',locals().get('item',{}).get('stage',''))),'meaning':self.tarot['meanings'].get(locals().get('number'),{}).get(locals().get('position'),''),'skill':locals().get('skill',''),'target':locals().get('target',''),'roll':locals().get('r','')}
        template=config.get('texts',{}).get(key)
        if template:text=re.sub(r'\{([^{}]+)\}',lambda match:str(variables.get(match.group(1),'')),template)
        result['text']=clean(text,420 if key=='help' or template else 140)
        if image and config['images_enabled']:
            path=Path(image).resolve()
            if path.is_relative_to(ASSETS.resolve()) and path.is_file():result['image']=str(path)
        return result
    def get_json(self,url,body=None):
        request=urllib.request.Request(url,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json','User-Agent':'NueBot/1.0'})
        with self.net.open(request,timeout=12) as response:
            raw=response.read(1024*1024+1)
        if len(raw)>1024*1024:raise ValueError('Plugin response too large')
        return json.loads(raw)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None
