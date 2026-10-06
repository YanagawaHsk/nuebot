"""CI-only GitHub release publication through the normal Actions token."""
import base64,json,os,urllib.request,urllib.error
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
repo=os.environ['GITHUB_REPOSITORY'];token=os.environ['GITHUB_TOKEN'];tag=os.environ['RELEASE_TAG']
def request(path,data=None,method='GET',binary=False):
    url=path if path.startswith('https://uploads.github.com/') else 'https://api.github.com/repos/'+repo+path
    body=data if binary else json.dumps(data).encode() if data is not None else None
    req=urllib.request.Request(url,data=body,method=method,headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','Content-Type':'application/zip' if binary else 'application/json','User-Agent':'NueBot-Release'})
    with urllib.request.urlopen(req,timeout=60) as response:return json.load(response)
try:release=request('/releases/tags/'+tag)
except urllib.error.HTTPError as error:
    if error.code!=404:raise
    release=request('/releases',{'tag_name':tag,'name':'小小鵺 '+tag,'body':(ROOT/'CHANGELOG.md').read_text(encoding='utf-8'),'draft':True,'prerelease':False},'POST')
if not release['draft'] and release.get('assets'):raise RuntimeError('Release already published; existing assets will not be replaced')
existing={a['name'] for a in release.get('assets',[])}
for path in (ROOT/'dist').iterdir():
    if path.name in existing:continue
    request(release['upload_url'].split('{',1)[0]+'?name='+path.name,path.read_bytes(),'POST',True)
request('/releases/'+str(release['id']),{'draft':False},'PATCH')
manifest=ROOT/'dist/latest.json'
try:old=request('/contents/latest.json?ref=main');sha=old['sha']
except urllib.error.HTTPError as error:
    if error.code!=404:raise
    sha=None
body={'message':'Publish '+tag+' update manifest','content':base64.b64encode(manifest.read_bytes()).decode(),'branch':'main'}
if sha:body['sha']=sha
request('/contents/latest.json',body,'PUT')
print('Release published and main/latest.json updated.')
