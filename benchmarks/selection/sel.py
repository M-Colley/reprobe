import collections
import json

import requests

res=json.load(open('res.json'))
el=[x for x in res if not x['why']]
el.sort(key=lambda x:(x['r']['title'].casefold(),x['r']['conf']))
sel=el[::3]
print(len(el),len(sel),collections.Counter(';'.join(x['badges']) for x in sel),collections.Counter(x['r']['listing'] for x in sel),collections.Counter(x['r']['conf'] for x in sel))
for x in sel:
    u=x['v']['link']
    try: r=requests.get(u,timeout=60,allow_redirects=True,headers={'User-Agent':'Mozilla/5.0'}); x['http']=r.status_code
    except Exception as e: x['http']=type(e).__name__
print(collections.Counter(x['http'] for x in sel))
json.dump(dict(el=el,sel=sel),open('sel.json','w'))
for x in sel: print(x['http'],x['r']['conf'],x['badges'],x['v']['lang'],round(x['v']['pyr'],2),x['v']['link'])
