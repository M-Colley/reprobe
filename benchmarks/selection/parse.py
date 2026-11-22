import yaml,glob,re,os,json,sys
rows=[]
for root in ['sys','sec']:
  for f in sorted(glob.glob(f'{root}/_conferences/*/results.md')):
    t=open(f,encoding='utf8').read()
    m=re.match(r'---\n(.*?)\n---',t,re.S)
    if not m: print('nofm',f);continue
    try: d=yaml.safe_load(m.group(1))
    except Exception as e: print('yamlerr',f,e);continue
    arts=d.get('artifacts') or []
    imgs=sorted({v for k,v in d.items() if k.endswith('_img')})
    conf=f.replace(chr(92),'/').split('/')[-2]
    print(root,conf,len(arts),imgs)
    for a in arts: rows.append(dict(listing=root,conf=conf,**a))
json.dump(rows,open('all.json','w'))
