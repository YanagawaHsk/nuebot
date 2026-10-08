"""Strict package allowlist: private configuration and media can never enter the zip."""
import ast,hashlib,json,os,re,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
FILES=['model_input.py','model_diagnostics.py','model_reservoir.py','reply_retry.py','model_output.py','moderation_intake.py','panel_endpoint.py','runtime_advice.py','Open-Local-ControlCenter.cmd','conversation_flow.py','model_gate.py','delivery_queue.py','error_log.py','ai_guard.py','panel_auth.py','manage_access.py','login.html','custodian.html','bot.py','chat_control.py','control.py','group_workers.py','memory_learning.py','moderation.py','panel_settings.py','panel_server.py','snowluma_bridge.py','plugin_features.py','plugin_manager.py','shared_budget.py','update_checker.py','open_panel.pyw','panel.html','local_identity.py','setup_local.py','start-panel.ps1','Open-ControlCenter.cmd','version.json','requirements.txt','model.example.json','account.example.json','moderation.example.json','persona.example.txt','plugin-assets/import-manifest.json','README.md','CHANGELOG.md']
FILES.insert(0,'member_memory.py')
FILES.insert(0,'moderation_control.py')
FILES.insert(0,'http_body.py')

def build(repository=None):
    version=json.loads((ROOT/'version.json').read_text(encoding='utf-8'))['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+',version):raise ValueError('Invalid version')
    tag=os.environ.get('RELEASE_TAG','v'+version)
    if tag!='v'+version:raise ValueError('Tag and program version disagree')
    dist=ROOT/'dist';dist.mkdir(exist_ok=True);archive=dist/f'nuebot-{version}-windows.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as package:
        for name in FILES:
            path=(ROOT/name).resolve()
            if not path.is_relative_to(ROOT) or not path.is_file():raise ValueError('Invalid package file: '+name)
            data=path.read_text(encoding='utf-8')
            if path.suffix in ('.py','.pyw'):ast.parse(data)
            if re.search(r'\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{12,})',data):raise ValueError('Credential-like content in '+name)
            package.write(path,name)
    manifest={'app_id':'nuebot','version':version,'release_notes':'公开程序发布；账号配置保留本机。详情见 CHANGELOG.md。','download_url':f'https://github.com/{repository}/releases/download/v{version}/{archive.name}' if repository else '', 'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
    (dist/'latest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return archive,manifest
if __name__=='__main__':
    archive,manifest=build(os.environ.get('GITHUB_REPOSITORY'));print(str(archive));print('SHA256: '+manifest['sha256'])
