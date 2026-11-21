import json,re,sys
ans=sys.argv[1] if len(sys.argv)>1 else '/home/claude/kits/br/sample/answers.json'
tj=sys.argv[2] if len(sys.argv)>2 else '/home/claude/kits/br/sample/tests.json'
a=json.load(open(ans))['commits']
T={x['number']:x for x in json.load(open(tj))['tests']}
out={json.loads(l)['commit']:json.loads(l)['workers'] for l in open('/home/claude/dev/data/last_output.jsonl')}
def cost(ws): return (max(sum(round(T[n]['seconds']*1000) for n in w) for w in ws)+2000*len(ws))/1000 if ws else 0
log={m.group(1):m.group(0) for m in re.finditer(r'\] (\w+): (cands.*|BROAD.*)', open('/home/claude/dev/data/last_output.err').read())}
rows=[];tot=0;orc=0
for c in a:
    f=a[c]['failing'] or []
    o=(sum(T[n]['seconds'] for n in f)+2) if f else 0
    sel={n for w in out[c] for n in w}; miss=bool(set(f)-sel)
    k=cost(out[c])+300*miss; tot+=k; orc+=o
    rows.append((round(k-o,1),c,a[c].get('kind',''),round(k,1),round(o,1),'MISS' if miss else '',log.get(c,'')[len(c)+4:150]))
for r in sorted(rows,reverse=True)[:int(sys.argv[3]) if len(sys.argv)>3 else 12]: print(r)
print('total',round(tot,1),'oracle',round(orc,1))
