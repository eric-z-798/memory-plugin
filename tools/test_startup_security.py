"""Real synthetic demo processes: logs, argv, umask, cleanup and stale capability."""
import json,os,signal,socket,subprocess,sys,time,urllib.request,urllib.error
from pathlib import Path
from qa_auth import private_data_directory
ROOT=Path(__file__).resolve().parents[1]

def get(base,token):
 try:
  with urllib.request.urlopen(urllib.request.Request(base+'/api/groups',headers={'X-Xingshu-Token':token}),timeout=1) as r:return r.status
 except urllib.error.HTTPError as e:return e.code

with private_data_directory() as tmp:
 root=Path(tmp); data=root/'new-data'; args=root/'argv';pidfile=root/'browser-pid'
 fake=root/'browser';fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "'+str(args)+'"\nprintf "%s" "$$" > "'+str(pidfile)+'"\nsleep 3\n');fake.chmod(0o700)
 with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
 base=f'http://127.0.0.1:{port}'
 def start(token,auto=True):
  command=[sys.executable,'-c',"import os,secrets,run;os.umask(0o002);secrets.token_urlsafe=lambda n:"+repr(token)+";run.main()",'--demo','--data-dir',str(data),'--port',str(port)]
  if not auto:command+=['--no-browser']
  log=(root/('log-'+token)).open('w')
  proc=subprocess.Popen(command,cwd=ROOT,env=dict(os.environ,PYTHONUNBUFFERED='1',DISPLAY='synthetic',BROWSER=str(fake)),stdout=log,stderr=log,start_new_session=True)
  for _ in range(100):
   if proc.poll() is not None:raise AssertionError((root/('log-'+token)).read_text())
   try:
    if get(base,token)==200:return proc,log
   except OSError:pass
   time.sleep(.05)
  raise AssertionError('server not ready')
 processes=[]
 try:
  proc,log=start('synthetic-A');processes.append(proc)
  for _ in range(100):
   if args.exists():break
   time.sleep(.05)
  assert args.exists()
  argv=args.read_text();assert argv.startswith('file://') and 'synthetic-A' not in argv
  cmdline=Path('/proc')/pidfile.read_text()/'cmdline'
  assert b'synthetic-A' not in cmdline.read_bytes()
  assert 'synthetic-A' not in (root/'log-synthetic-A').read_text()
  assert data.stat().st_mode&0o777==0o700
  private=list(data.glob('.startup-*'));assert len(private)==1
  assert private[0].stat().st_mode&0o777==0o700
  for f in private[0].iterdir():assert f.stat().st_mode&0o777==0o600
  proc.terminate();proc.wait(timeout=8);log.close();assert proc.returncode==0
  assert not private[0].exists()
  proc,log=start('synthetic-B',False);processes.append(proc)
  stale=list(data.glob('.startup-*'));proc.kill();proc.wait();log.close();assert stale[0].exists()
  proc,log=start('synthetic-C',False);processes.append(proc)
  assert get(base,'synthetic-B')==401 and get(base,'synthetic-C')==200
  proc.terminate();proc.wait(timeout=8);log.close()
  # Old unknown files are not overwritten or followed on restart.
  assert 'synthetic-B' in (stale[0]/'startup-url.txt').read_text()
  data.chmod(0o775)
  result=subprocess.run([sys.executable,'run.py','--demo','--data-dir',str(data),'--no-browser'],cwd=ROOT,text=True,capture_output=True)
  assert result.returncode!=0 and 'Traceback' not in result.stderr and '无法启动' in result.stderr
  assert data.stat().st_mode&0o777==0o775
  print(json.dumps({'log_no_token':True,'browser_argv_no_token':True,'files_600_directory_700':True,'umask_002_started':True,'sigterm_cleanup':True,'sigkill_residue_expired':True,'existing_775_rejected_without_chmod':True,'other_os_user_tested':False}))
 finally:
  for proc in processes:
   if proc.poll() is None:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
