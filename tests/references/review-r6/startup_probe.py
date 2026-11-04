import os,sys,tempfile,subprocess,time,socket,json,signal
from pathlib import Path
ROOT=Path('/workspace/memory-plugin')
os.umask(0o022)
with tempfile.TemporaryDirectory() as tmp:
 p=Path(tmp);fake=p/'browser';args=p/'argv'
 fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "'+str(args)+'"\nsleep 5\n');fake.chmod(0o700)
 with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
 env=dict(os.environ,PYTHONUNBUFFERED='1',DISPLAY='synthetic',BROWSER=str(fake))
 cmd=[sys.executable,'-c',"import secrets,runpy;secrets.token_urlsafe=lambda n:'synthetic-startup-token';runpy.run_path('run.py',run_name='__main__')",'--demo','--port',str(port),'--data-dir',str(p/'data')]
 with (p/'log').open('w') as log:
  proc=subprocess.Popen(cmd,cwd=ROOT,env=env,stdout=log,stderr=log,start_new_session=True)
  try:
   for _ in range(100):
    if args.exists() or proc.poll() is not None:break
    time.sleep(.05)
   print(json.dumps({'token_in_redirected_log':'synthetic-startup-token' in (p/'log').read_text(),'token_in_browser_argv':args.exists() and 'synthetic-startup-token' in args.read_text(),'log_mode':oct((p/'log').stat().st_mode&0o777)}),flush=True)
  finally:
   os.killpg(proc.pid,signal.SIGTERM);proc.wait()
 result=subprocess.run([sys.executable,'-c',"import os;os.umask(0o002);from aigc.security import DataDirectoryLock;from pathlib import Path;\nwith DataDirectoryLock(Path("+repr(str(p/'new'))+")):print('started')"],cwd=ROOT,text=True,capture_output=True)
 print('UMASK_EXIT',result.returncode,'MODE',oct((p/'new').stat().st_mode&0o777),flush=True)
 print(result.stdout+result.stderr)
