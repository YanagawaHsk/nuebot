import json,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
import panel_settings,group_workers,shared_budget
HALT=ROOT/'STOP'
action=sys.argv[1] if len(sys.argv)>1 else 'status'
if action=='stop':
    HALT.write_text('Stopped from local control',encoding='utf-8');print('已请求停止所有群后台。')
elif action=='start':
    config=panel_settings.load();groups=[row['group_id'] for row in config['groups'] if row['enabled']]
    if not groups:print('请先在插件页面选择至少一个参与群并保存。');raise SystemExit(1)
    if group_workers.locked(ROOT/'runner.lock'):print('旧版后台尚在运行，请先停止后再启动。');raise SystemExit(1)
    for directory in [ROOT]+[group_workers.directory(gid) for gid in groups]:
        for name in ('last-send.json','last-moderation.json'):
            path=directory/name
            if path.exists() and json.loads(path.read_text(encoding='utf-8')).get('state') in ('UNKNOWN','PENDING'):
                print('上次发送或管理结果待核实，请先检查QQ，避免重复操作。');raise SystemExit(1)
    if not shared_budget.PATH.exists():
        from datetime import datetime
        stamps=[]
        for line in (ROOT/'events.log').read_text(encoding='utf-8').splitlines() if (ROOT/'events.log').exists() else []:
            if ' message_sent ' not in line:continue
            try:stamp=datetime.strptime(line[:23],'%Y-%m-%d %H:%M:%S,%f').timestamp()
            except ValueError:continue
            if stamp>time.time()-3600:stamps.append(stamp)
        with shared_budget.transaction() as budget:budget['message']=stamps
    if HALT.exists():HALT.unlink()
    launched=[]
    for group in groups:
        directory=group_workers.directory(group);directory.mkdir(parents=True,exist_ok=True)
        if group_workers.locked(directory/'runner.lock'):continue
        with (directory/'runner-output.log').open('a',encoding='utf-8') as log:
            subprocess.Popen([sys.executable,'-X','utf8',str(ROOT/'bot.py'),'--group',str(group)],cwd=ROOT,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        launched.append(group)
    print('已启动群后台：'+('、'.join(map(str,launched)) if launched else '所选群已在运行'))
else:print(json.dumps({'groups':group_workers.statuses(),'stop_requested':HALT.exists()},ensure_ascii=False))
