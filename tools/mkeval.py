import json,os,sys,shutil
# usage: mkeval.py OUT_DIR (PATCH_DIR LABELS)...
out=sys.argv[1]; shutil.rmtree(out,ignore_errors=True); os.makedirs(out+'/commits')
shutil.copy('/home/claude/kits/br/sample/tests.json',out+'/tests.json')
ans={}
args=sys.argv[2:]
for pd,lf in zip(args[::2],args[1::2]):
    for c,v in json.load(open(lf))['commits'].items():
        if v.get('failing') is None: continue
        shutil.copy(f'{pd}/{c}.patch',f'{out}/commits/{c}.patch'); ans[c]={'failing':v['failing'],'kind':'mut'}
json.dump({'commits':ans},open(out+'/answers.json','w')); print(len(ans),'commits')
