import json,re,collections
from urllib.parse import urlparse
rows=json.load(open('all.json'))
sysc=re.compile(r'^(eurosys20(2[1-6])|sosp20(19|2[1-6])|sc2021|atc2026|cais2026)$')
secc=re.compile(r'^acsac20(1[7-9]|2[0-3])$')
pool=[r for r in rows if (r['listing']=='sys' and sysc.match(r['conf'])) or (r['listing']=='sec' and secc.match(r['conf']))]
def host(u):
  u=(u or '').strip()
  if not u: return 'none'
  h=urlparse(u).netloc.lower().replace('www.','')
  if h in('doi.org','dx.doi.org'):
    d=u.split('doi.org/')[1]
    for p,n in [('10.5281','zenodo'),('10.6084','figshare'),('10.5061','dryad'),('10.17605','osf'),('10.7910','dataverse')]:
      if d.startswith(p): return n+'-doi'
    return 'doi-other'
  return h
c=collections.Counter(host(r.get('artifact_url')) for r in pool)
print(len(pool),c.most_common())
json.dump(pool,open('pool.json','w'))
print(collections.Counter(r['conf'] for r in pool))
print(collections.Counter(str(r.get('badges')) for r in pool))
