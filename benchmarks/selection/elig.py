import json,collections,re
a=json.load(open('assess.json')); pool=json.load(open('pool.json'))
def reason(v):
    s=v['status']
    if s=='nolink': return '1 no artifact link in listing'
    if s=='unsupported_host': return '2 host not supported by reprobe'
    if s=='dead': return '3 link does not resolve'
    if s in('unread','nofiles'): return '4 language not determinable (tar/other archive, or no source files)'
    if v['pyr']<0.5: return '5 not predominantly Python/R'
    if v.get('compose'): return '6 docker-compose at repo root'
    if v.get('gpu_files'): return '7 GPU keyword in README/dependency file'
    return None
def norm(b):
    return sorted({x.strip().lower() for x in re.split(r'[,;]',b) if x.strip()},key=['available','functional','reusable','reproduced','replicated'].index)
res=[]
for r in pool:
    k=f"{r['conf']}|{r['title']}"; v=a[k]; res.append(dict(r=r,v=v,why=reason(v),badges=norm(r['badges'])))
c=collections.Counter(x['why'] or 'eligible' for x in res); print(len(res),sorted(c.items()))
el=[x for x in res if not x['why']]
print(collections.Counter(x['r']['listing'] for x in el),collections.Counter(x['r']['conf'] for x in el))
print(collections.Counter(';'.join(x['badges']) for x in el))
print(collections.Counter(x['v']['host'] for x in el))
json.dump(res,open('res.json','w'))
