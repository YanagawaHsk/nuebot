"""Recoverable uninstall; only fixed, owned asset paths can be moved."""
import json,threading,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent
ASSETS=ROOT/'plugin-assets'
TRASH=ROOT/'plugin-trash'
REGISTRY=ROOT/'plugin-registry.json'
LOCK=threading.RLock()
OWNED={'fortune':['FortuneDraw'],'tarot':['TarotReader','tarot-data.json','tarot-cache'],'food':['WhatToEat'],'music':['TouhouOriginalMusicDraw'],'spell':['TouhouSpellCardDraw'],'anime':['image-filter.json','anime-cache']}
VALID={'dice','jrrp','fortune','tarot','food','music','spell','hourly','help','say','image_reply','jm_joke','bilibili','anime'}
def registry():
    try:
        data=json.loads(REGISTRY.read_text(encoding='utf-8'))
        if not isinstance(data,dict) or not isinstance(data.get('removed',{}),dict):raise ValueError('插件安装记录损坏')
        return data
    except FileNotFoundError:return {'removed':{}}
def installed(key):
    try:return key not in registry()['removed']
    except (ValueError,OSError):return False
def write(data):
    tmp=REGISTRY.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(REGISTRY)
def safe_path(base,name):
    path=(base/name).resolve()
    if not path.is_relative_to(base.resolve()) or path==base.resolve():raise ValueError('插件路径越界')
    return path
def uninstall(key):
    if key not in VALID:raise ValueError('只能卸载独立适配插件，不能删除聊天或系统核心')
    with LOCK:
        data=registry()
        if key in data['removed']:return {'ok':True,'message':'该插件已经卸载'}
        folder=TRASH/(key+'-'+str(time.time_ns()));folder.mkdir(parents=True,exist_ok=False)
        record={'at':time.strftime('%Y-%m-%d %H:%M:%S'),'folder':folder.name,'assets':[]}
        # Mark unavailable first, so in-flight requests fail their send guard.
        data['removed'][key]=record;write(data)
        try:
            for name in OWNED.get(key,[]):
                source=safe_path(ASSETS,name);destination=safe_path(folder,name)
                if source.exists():source.rename(destination);record['assets'].append(name);write(data)
        except Exception:
            record['error']='部分素材未移动，插件已停止；可以恢复后重试';write(data);raise ValueError(record['error']) from None
        return {'ok':True,'message':'已从所有群卸载，素材与安装记录可恢复；各群文案和限额保留'}
def restore(key):
    if key not in VALID:raise ValueError('插件名称不正确')
    with LOCK:
        data=registry();record=data['removed'].get(key)
        if not record:return {'ok':True,'message':'插件已经安装'}
        folder=safe_path(TRASH,record['folder'])
        for name in record['assets']:
            if name not in OWNED.get(key,[]):raise ValueError('安装记录包含未知素材路径')
            source=safe_path(folder,name);destination=safe_path(ASSETS,name)
            if destination.exists() and source.exists():raise ValueError('已有同名素材，保留两份文件，请先检查')
            if not source.exists() and not destination.exists():raise ValueError('恢复素材缺失，保留卸载状态，请检查本地回收区')
            if source.exists():source.rename(destination)
        data['removed'].pop(key);write(data)
        return {'ok':True,'message':'插件已恢复；保持关闭，请到各群配置中重新启用'}
