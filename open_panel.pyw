"""Open the control center; remote mode never starts local services."""
import argparse,json,socket,subprocess,sys,time,urllib.error,urllib.request,webbrowser
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
import panel_endpoint
URL=panel_endpoint.ORIGIN+'/'
APP_ID='nuebot'

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

def port_in_use():
    try:
        with socket.create_connection(('127.0.0.1',panel_endpoint.PORT),timeout=.4):return True
    except ConnectionRefusedError:return False
    except OSError:return True  # Ambiguous timeouts must not start over another service.

def probe_service():
    """Identify the app even when the root page is an anonymous login screen."""
    responding=False
    try:
        with opener.open(URL+'api/health',timeout=1) as response:
            responding=True
            health=json.loads(response.read(8192).decode('utf-8'))
        if isinstance(health,dict) and health.get('app_id')==APP_ID:
            if health.get('control_center') is True and health.get('bridge_port')==panel_endpoint.BRIDGE_PORT:
                return {'state':'ready','version':str(health.get('version',''))[:80]}
            return {'state':'legacy'}
        if isinstance(health,dict) and health.get('app_id'):return {'state':'foreign'}
    except urllib.error.HTTPError:responding=True
    except (OSError,ValueError,UnicodeError):pass
    try:
        with opener.open(URL,timeout=1) as response:
            responding=True
            page=response.read(300000).decode('utf-8',errors='replace')
        if '小小鵺' in page or '鵺 · 控制室' in page:return {'state':'legacy'}
    except urllib.error.HTTPError:responding=True
    except OSError:pass
    return {'state':'foreign' if responding or port_in_use() else 'absent'}

def available():return probe_service()['state'] in ('ready','legacy')

def notify(message,error=False):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None,message,'鵺 · 统一控制中心',0x10 if error else 0x40)
    except (AttributeError,OSError):
        stream=sys.stderr if error else sys.stdout
        if stream:print(message,file=stream)

def start_local_panel():
    server=ROOT/'panel_server.py'
    if not server.is_file():raise FileNotFoundError('找不到控制中心程序。')
    with (ROOT/'panel-output.log').open('a',encoding='utf-8') as log:
        subprocess.Popen([sys.executable,'-X','utf8',str(server)],cwd=ROOT,stdout=log,stderr=log,
                         creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

def main(argv=None):
    parser=argparse.ArgumentParser(description='打开鵺统一控制中心')
    parser.add_argument('--mode',choices=('local','remote'),default='local')
    parser.add_argument('--remote',action='store_true',help='只打开已有隧道页面')
    parser.add_argument('--check',action='store_true',help='仅检查，不启动或打开网页')
    parser.add_argument('--no-browser',action='store_true',help='启动并检查控制中心，不打开网页')
    parser.add_argument('--section',choices=('overview','qq'),default='overview',help='打开指定控制室页面')
    args=parser.parse_args(argv);mode='remote' if args.remote else args.mode
    state=probe_service()
    if args.check:
        if sys.stdout:print(json.dumps(state,ensure_ascii=True))
        return 0 if state['state'] in ('ready','legacy') else 1
    if state['state']=='absent' and mode=='local':
        try:start_local_panel()
        except OSError:
            notify('控制中心启动失败，请检查程序目录与 panel-output.log。',error=True);return 1
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            state=probe_service()
            if state['state']!='absent':break
            time.sleep(.15)
    if state['state']=='legacy':
        notify('当前管理服务是旧版本，已保留原服务。请升级运行电脑上的鵺程序后使用统一 QQ 控制台；现在只打开已有网页。')
    elif state['state']!='ready':
        if state['state']=='foreign':message='管理端口已被其他服务占用或响应异常，未启动新的服务。请检查已有管理连接。'
        elif mode=='remote':message='远程管理连接尚未就绪，请先运行统一远程快捷方式建立隧道。未启动本机服务。'
        else:message='控制中心尚未启动，请检查 panel-output.log 后重试。'
        notify(message,error=True);return 1
    if not args.no_browser:
        webbrowser.open(URL+('#snowluma-tab' if args.section=='qq' else ''))
    return 0

if __name__=='__main__':raise SystemExit(main())
