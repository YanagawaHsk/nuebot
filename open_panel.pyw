import subprocess,sys,time,urllib.request,webbrowser
from pathlib import Path
ROOT=Path(__file__).resolve().parent
URL='http://127.0.0.1:5100/'
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def available():
    try:
        with opener.open(URL,timeout=1) as r:return '鵺 · 控制室' in r.read(300000).decode('utf-8')
    except Exception:return False
if not available():
    with (ROOT/'panel-output.log').open('a',encoding='utf-8') as log:
        subprocess.Popen([sys.executable,'-X','utf8',str(ROOT/'panel_server.py')],cwd=ROOT,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(40):
        if available():break
        time.sleep(.15)
if available():webbrowser.open(URL)
