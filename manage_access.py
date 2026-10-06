"""Local recovery only; run on the trusted host after stopping the panel."""
import getpass,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from panel_auth import Store,password_hash

def main():
    if sys.argv[1:]!=['reset-admin']:raise SystemExit('用法：python manage_access.py reset-admin')
    store=Store();data=store.read()
    if not data['owner']:raise SystemExit('尚未创建主管理员，请在登录页面完成首次设置')
    print('本机恢复主管理员密码。请先退出面板服务；现有登录将失效。')
    if input('输入 RESET 确认：').strip()!='RESET':raise SystemExit('已取消')
    first=getpass.getpass('新密码（至少12个字符）：')
    if first!=getpass.getpass('再次输入新密码：'):raise SystemExit('两次密码不一致')
    data['users'][data['owner']]['password']=password_hash(first);store.write(data)
    store.audit(data['owner'],'local_password_recovery');print('密码已恢复，请重新启动面板并登录。')

if __name__=='__main__':main()
