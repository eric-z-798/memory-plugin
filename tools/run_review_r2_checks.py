"""Capture reproducible R2 QA commands, raw output and exit codes (test dependencies only)."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path(__file__).resolve().parents[1]
out = Path(os.environ.get('XINGSHU_QA_OUTPUT', '/tmp/xingshu-r2-evidence')).resolve()
out.mkdir(parents=True, exist_ok=True)
sha = subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
status = subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True)
commands = [
    ['python3','-m','unittest','discover','-s','tests'],
    *[['python3','-m','unittest','discover','-s','tests','-p',f'{name}.py'] for name in ('test_review_r1','test_review_r1_followup','test_review_r1_sequence','test_review_r1_json_identity','test_review_r2')],
    ['node','tools/test_ui_dom.cjs'],
    *[['python3',f'tools/{name}.py'] for name in ('test_browser','test_routing_browser','test_setup_browser','test_review_r1_browser','test_review_r1_browser')],
    ['git','diff','--check'],
]
results = []
for index, command in enumerate(commands):
    env=os.environ.copy()
    env['XINGSHU_QA_OUTPUT']=str(out/('browser-mixed' if index==11 else 'browser-original'))
    env['XINGSHU_QA_MIXED']='1' if index==11 else '0'
    name=f'{index:02d}-{Path(command[-1]).stem}'
    started=time.time()
    with (out/(name+'.log')).open('w') as log:
        result=subprocess.run(command,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT)
    results.append(dict(command=command,cwd=str(root),sha=sha,initial_worktree_status=status,
        environment_overrides={key:env.get(key) for key in ('NODE_PATH','XINGSHU_QA_OUTPUT','XINGSHU_QA_MIXED')},
        exit_code=result.returncode,seconds=time.time()-started,output=name+'.log'))
    (out/'manifest.json').write_text(json.dumps(results,indent=2)+'\n')
    print(name,result.returncode,flush=True)
for item in out.glob('*.log'):
    item.with_suffix('.sha256').write_text(hashlib.sha256(item.read_bytes()).hexdigest()+'\n')
sys.exit(0 if all(r['exit_code']==0 for r in results) else 1)
