"""Capture Security/product boundaries plus R4 and all browser regressions without discarding failures."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root=Path(__file__).resolve().parents[1]
out=Path(os.environ.get('XINGSHU_QA_OUTPUT','/tmp/xingshu-security-evidence')).resolve()
out.mkdir(parents=True,exist_ok=True)
manifest=out/'manifest.json'
if manifest.exists():raise SystemExit('Evidence already exists; use a new directory, never overwrite a run.')
sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
initial_status=subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True)
commands=[('python-all',['python3','-m','unittest','discover','-s','tests'],{}),
          ('dom',['node','tools/test_ui_dom.cjs'],{})]
for name in ('test_review_r1','test_review_r1_followup','test_review_r1_sequence','test_review_r1_json_identity','test_review_r2','test_review_r3','test_review_r4','test_security_boundaries','test_review_r5'):
    commands.append((name,['python3','-m','unittest','discover','-s','tests','-p',name+'.py'],{}))
for name in ('test_browser','test_routing_browser','test_setup_browser','test_review_r3_browser','test_review_r4_browser','test_security_browser','test_review_r5_browser'):
    commands.append((name,['python3','tools/'+name+'.py'],{}))
for mixed in ('0','1'):
    for iteration in range(1,2):
        commands.append((f'atlas-{mixed}-{iteration:02d}',['python3','tools/test_review_r1_browser.py'],{'XINGSHU_QA_MIXED':mixed}))
commands.append(('diff-check',['git','diff','--check'],{}))
results=[]
for name,command,extra in commands:
    current=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    if current!=sha:raise SystemExit('HEAD changed; preserving incomplete evidence and stopping.')
    env=os.environ.copy();env.update(extra);env['XINGSHU_QA_OUTPUT']=str(out/name/'browser-images')
    started=time.time()
    with (out/(name+'.log')).open('x') as log:
        completed=subprocess.run(command,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT)
    results.append({'sha':sha,'initial_worktree_status':initial_status,'command':command,'cwd':str(root),
        'environment_overrides':{k:env.get(k) for k in ('NODE_PATH','XINGSHU_QA_OUTPUT','XINGSHU_QA_MIXED')},
        'exit_code':completed.returncode,'seconds':time.time()-started,'output':name+'.log',
        'output_sha256':hashlib.sha256((out/(name+'.log')).read_bytes()).hexdigest()})
    manifest.write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    print(name,completed.returncode,flush=True)
final_status=subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True)
(out/'final-status.json').write_text(json.dumps({'sha':sha,'status':final_status},indent=2)+'\n')
sys.exit(0 if all(r['exit_code']==0 for r in results) and final_status==initial_status else 1)
