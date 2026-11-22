import collections
import concurrent.futures as cf
import json
import os
import re
import subprocess

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SES=requests.Session(); SES.mount('https://',HTTPAdapter(max_retries=Retry(total=10,backoff_factor=3,status_forcelist=[429,502,503],respect_retry_after_header=True)))
from urllib.parse import urlparse

from remotezip import RemoteZip

TOK=subprocess.check_output(['gh','auth','token'],text=True).strip()
GH={'Authorization':'Bearer '+TOK,'Accept':'application/vnd.github+json'}
pool=json.load(open('pool.json'))
NONCODE={'Shell','Makefile','Dockerfile','CMake','TeX','HTML','CSS','Batchfile','PowerShell','Roff','Nix','Starlark','M4'}
PYR={'Python','Jupyter Notebook','R','RMarkdown'}
EXT={'.py':'Python','.ipynb':'Jupyter Notebook','.r':'R','.rmd':'RMarkdown','.qmd':'RMarkdown',
 '.c':'C','.h':'C','.cc':'C++','.cpp':'C++','.hpp':'C++','.cxx':'C++','.cu':'CUDA','.cuh':'CUDA','.java':'Java','.go':'Go','.rs':'Rust',
 '.js':'JavaScript','.ts':'TypeScript','.jl':'Julia','.scala':'Scala','.kt':'Kotlin','.swift':'Swift','.m':'MATLAB','.cs':'C#','.rb':'Ruby','.php':'PHP','.pl':'Perl','.lua':'Lua','.v':'Verilog','.sv':'Verilog','.ml':'OCaml','.hs':'Haskell','.f90':'Fortran','.f':'Fortran','.asm':'Assembly','.s':'Assembly','.dart':'Dart','.sol':'Solidity','.tf':'HCL','.cl':'OpenCL'}
GPU=re.compile(r'cuda|nvidia|cudnn|\bgpus?\b',re.I)
DEPF=re.compile(r'^(readme.*|requirements.*\.txt|environment\.ya?ml|pyproject\.toml|setup\.py)$',re.I)
COMPOSE=re.compile(r'^(docker-)?compose.*\.ya?ml$',re.I)
def link(r):
    u=(r.get('artifact_url') or r.get('github_url') or r.get('repository_url') or '').strip()
    if u.upper()=='MISSING': return ''
    if re.match(r'^10\.\d+/',u): u='https://doi.org/'+u
    elif not re.match(r'^https?://',u) and u: u='https://'+u
    return u
def summarize(langs):
    items=[(v,k) for k,v in langs.items() if k not in NONCODE]
    tot=sum(v for v,k in items)
    if tot==0: return None,0,{}
    items.sort(reverse=True)
    py=sum(v for v,k in items if k in PYR)
    return items[0][1],py/tot,{k:round(v/tot,3) for v,k in items[:4]}
def gh_assess(u):
    p=urlparse(u).path.strip('/').split('/')
    if len(p)<2: return dict(status='dead',why='no repo path')
    o,rn=p[0],re.sub(r'\.git$','',p[1])
    s=requests.get(f'https://api.github.com/repos/{o}/{rn}',headers=GH,timeout=30)
    if s.status_code!=200: return dict(status='dead',why=f'GitHub API {s.status_code}')
    m=s.json(); d=dict(repo=f'{o}/{rn}',archived=m.get('archived'),size_kb=m.get('size'),subpath=len(p)>2)
    langs=requests.get(f'https://api.github.com/repos/{o}/{rn}/languages',headers=GH,timeout=30).json()
    d['lang'],d['pyr'],d['top']=summarize(langs)
    ents=requests.get(f'https://api.github.com/repos/{o}/{rn}/contents/',headers=GH,timeout=30).json()
    if not isinstance(ents,list): ents=[]
    d['compose']=any(COMPOSE.match(e['name']) for e in ents if e['type']=='file')
    gpu=[]
    for e in ents:
        if e['type']=='file' and DEPF.match(e['name']) and e.get('size',0)<500000:
            try:
                if GPU.search(requests.get(e['download_url'],timeout=30).text): gpu.append(e['name'])
            except Exception: pass
    d['gpu_files']=gpu; d['status']='ok'; return d
def zen_assess(u):
    m=re.search(r'zenodo\.(\d+)',u) or re.search(r'/records?/(\d+)',urlparse(u).path)
    if not m: return dict(status='dead',why='no zenodo id')
    rid=m.group(1)
    s=SES.get(f'https://zenodo.org/api/records/{rid}',timeout=60)
    if s.status_code!=200: return dict(status='dead',why=f'Zenodo API {s.status_code}')
    j=s.json(); files=j.get('files',[]); d=dict(rid=rid,nfiles=len(files),size=sum(f['size'] for f in files))
    langs={}; gpu=[]; st={'compose':False}
    def addname(n,size,isroot,reader):
        ext=os.path.splitext(n)[1].lower()
        if ext in EXT: langs[EXT[ext]]=langs.get(EXT[ext],0)+size
        base=os.path.basename(n)
        if isroot:
            if COMPOSE.match(base): st['compose']=True
            if DEPF.match(base) and size<500000:
                try:
                    if GPU.search(reader()): gpu.append(base)
                except Exception: pass
    unread=[]
    for f in files:
        k=f['key']; url=f'https://zenodo.org/api/records/{rid}/files/{k}/content'
        if k.lower().endswith('.zip'):
            try:
                with RemoteZip(url,session=SES) as z:
                    infos=[i for i in z.infolist() if not i.is_dir()]
                    tops={i.filename.split('/')[0] for i in infos}
                    pre=(list(tops)[0]+'/') if len(tops)==1 and all('/' in i.filename for i in infos) else ''
                    for i in infos:
                        rel=i.filename[len(pre):]
                        addname(i.filename,i.file_size,'/' not in rel,lambda i=i:z.read(i.filename).decode('utf8','ignore'))
            except Exception as e: unread.append(f'{k}:{type(e).__name__}')
        elif re.search(r'\.(tar|tgz|tar\.gz|tar\.xz|7z|rar|gz)$',k.lower()): unread.append(k)
        else: addname(k,f['size'],True,lambda url=url:SES.get(url,timeout=60).text)
    d['unread']=unread
    d['lang'],d['pyr'],d['top']=summarize(langs)
    d['gpu_files']=gpu; d['compose']=st['compose']; d['status']='ok' if langs else ('unread' if unread else 'nofiles')
    return d
def assess(r):
    u=link(r)
    if not u: return dict(status='nolink')
    h=urlparse(u).netloc.lower().replace('www.','')
    try:
        if h=='github.com': return dict(host='github',**gh_assess(u))
        if (h in('doi.org','dx.doi.org') and '10.5281' in u) or 'zenodo.org' in h: return dict(host='zenodo',**zen_assess(u))
        if h=='gitlab.com':
            p=urlparse(u).path.strip('/').split('/tree')[0].split('/-/')[0]
            s=requests.get('https://gitlab.com/api/v4/projects/'+requests.utils.quote(p,safe='')+'/languages',timeout=30)
            if s.status_code!=200: return dict(host='gitlab',status='dead',why=f'GitLab API {s.status_code}')
            top=sorted(((v,k) for k,v in s.json().items() if k not in NONCODE),reverse=True)
            tot=sum(v for v,k in top) or 1
            return dict(host='gitlab',status='ok',lang=top[0][1] if top else None,pyr=sum(v for v,k in top if k in PYR)/tot,top={k:v for v,k in top[:4]},gpu_files=[],compose=False,note='gitlab: GPU/compose not checked')
        return dict(host=h,status='unsupported_host')
    except Exception as e:
        return dict(host=h,status='error',why=f'{type(e).__name__}: {e}')
if __name__=='__main__':
    out=json.load(open('assess.json')) if os.path.exists('assess.json') else {}
    key=lambda r:f"{r['conf']}|{r['title']}"
    todo=[r for r in pool if key(r) not in out or out[key(r)].get('status') in('error','dead','unread','nofiles','unsupported_host','nolink') and (out[key(r)].get('host') in('zenodo','',None,'doi.org') or out[key(r)].get('why','').startswith('Zenodo') or out[key(r)]['status']=='nolink')]
    print(len(todo),'todo',flush=True)
    with cf.ThreadPoolExecutor(1) as ex:
        for r,res in zip(todo,ex.map(assess,todo),strict=True):
            out[key(r)]=dict(link=link(r),**res)
    json.dump(out,open('assess.json','w'),indent=1)
    print(collections.Counter(v['status'] for v in out.values()))
