"""Initialize missing local files; never replace an existing owner's configuration."""
import json,shutil,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
if not (ROOT/'account.json').exists():
    account={}
    for key,label in [('bot_id','机器人QQ'),('owner_id','你的主人QQ'),('default_group','默认群号')]:
        value=int(input(label+'：').strip())
        if not 10000<=value<=999999999999:raise ValueError('账号或群号格式不正确')
        account[key]=value
    account['onebot_config']=input('SnowLuma OneBot配置文件路径（留空使用相邻SnowLuma-v1.14.19目录）：').strip().strip('"')
    (ROOT/'account.json').write_text(json.dumps(account,ensure_ascii=False,indent=2),encoding='utf-8')
account=json.loads((ROOT/'account.json').read_text(encoding='utf-8'))
for target,example in [('model.json','model.example.json'),('persona.txt','persona.example.txt')]:
    if not (ROOT/target).exists():shutil.copy2(ROOT/example,ROOT/target)
if not (ROOT/'moderation.json').exists():
    policy=json.loads((ROOT/'moderation.example.json').read_text(encoding='utf-8'))
    policy.update(group_id=account['default_group'],protected_accounts=[account['owner_id'],account['bot_id']])
    (ROOT/'moderation.json').write_text(json.dumps(policy,ensure_ascii=False,indent=2),encoding='utf-8')
if not (ROOT/'sticker-catalog.json').exists():(ROOT/'sticker-catalog.json').write_text('[]',encoding='utf-8')
if not (ROOT/'settings.json').exists():
    import panel_settings
    panel_settings.save(panel_settings.defaults())
print('初始化完成。请在本机 model.json 填写API地址、模型和密钥，再运行 start-panel.ps1。QQ与SnowLuma需由你自行安装和登录。')
