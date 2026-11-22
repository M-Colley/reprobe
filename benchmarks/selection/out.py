import collections
import csv
import json
import os
import re

d=json.load(open('sel.json')); res=json.load(open('res.json'))
sel=d['sel']; selk={(x['r']['conf'],x['r']['title']) for x in sel}
VEN={'eurosys':'EuroSys','sosp':'SOSP','sc':'SC','atc':'USENIX ATC','cais':'CAIS','acsac':'ACSAC'}
def ven(c):
    m=re.match(r'([a-z]+)(\d{4})',c); return VEN[m.group(1)],m.group(2)
LU={'sys':'https://sysartifacts.github.io/','sec':'https://secartifacts.github.io/'}
out=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..') + '/'
with open(out+'acm-badged.csv','w',newline='',encoding='utf8') as f:
    w=csv.writer(f,lineterminator='\n')
    w.writerow('url,data,notes,venue,year,paper,badges,language,listing_url'.split(','))
    for x in sorted(sel,key=lambda x:(x['r']['conf'],x['r']['title'].casefold())):
        r,v=x['r'],x['v']; vn,y=ven(r['conf'])
        note=f"{v['host']}; py/R share {v['pyr']:.0%}; HTTP 200"
        w.writerow([v['link'],'',note,vn,y,' '.join(r['title'].split()),';'.join(x['badges']),v['lang'],f"{LU[r['listing']]}{r['conf']}/results.html"])
seen=collections.Counter(x['r']['listing'] for x in res)
tot=collections.Counter(x['why'] or 'eligible' for x in res)
def esc(s): return ' '.join(s.split()).replace('|','\\|')
L=[open('md_head.txt',encoding='utf8').read().replace('{COUNTS}','\n'.join(f"| {k} | {n} |" for k,n in sorted(tot.items()))).replace('{PERLIST}','\n'.join(f"- {k}: {n} rows" for k,n in sorted(seen.items())))]
L.append('\n## Eligible but not sampled (70 rows)\n\n| conf | title | badges | link |\n|---|---|---|---|')
for x in d['el']:
    if (x['r']['conf'],x['r']['title']) not in selk: L.append(f"| {x['r']['conf']} | {esc(x['r']['title'])} | {';'.join(x['badges'])} | {x['v']['link']} |")
L.append('\n## Excluded rows (reason = first failing criterion)\n\n| conf | title | reason | link |\n|---|---|---|---|')
for x in sorted(res,key=lambda x:(x['why'] or '',x['r']['conf'],x['r']['title'])):
    if x['why']:
        lk=x['v'].get('link') or ''
        extra=''
        if x['why'][0]=='5': extra=f" ({x['v']['lang']}, py/R {x['v']['pyr']:.0%})"
        if x['why'][0]=='7': extra=' ('+','.join(x['v']['gpu_files'])+')'
        if x['why'][0]=='3': extra=f" ({x['v'].get('why')})"
        L.append(f"| {x['r']['conf']} | {esc(x['r']['title'])} | {x['why'][2:]}{extra} | {lk} |")
open(out+'acm-badged-selection.md','w',encoding='utf8').write('\n'.join(L)+'\n')
print(tot,seen,len(sel))
