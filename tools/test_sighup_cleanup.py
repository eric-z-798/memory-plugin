"""POSIX synthetic processes: three HUP exits, plus replacement-path preservation."""
import json,os,signal,socket,subprocess,sys,time,urllib.request,urllib.error
from pathlib import Path
from qa_auth import private_data_directory
ROOT=Path(__file__).resolve().parents[1]
with private_data_directory() as tmp:
 root=Path(tmp);data=root/'data';codes=[]
 def start():
  with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
  proc=subprocess.Popen([sys.executable,'-c',"import secrets,run;secrets.token_urlsafe=lambda n:'synthetic-hup';run.main()",'--demo','--no-browser','--port',str(port),'--data-dir',str(data)],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  for _ in range(100):
   if proc.poll() is not None:raise AssertionError(proc.communicate())
   try:
    req=urllib.request.Request(f'http://127.0.0.1:{port}/api/info',headers={'X-Xingshu-Token':'synthetic-hup'})
    with urllib.request.urlopen(req,timeout=.2) as r:
     if r.status==200:return proc
   except OSError:time.sleep(.05)
  proc.kill();proc.communicate();raise AssertionError('server not ready')
 def finish(proc):
  try:
   proc.send_signal(signal.SIGHUP);out,err=proc.communicate(timeout=8)
   assert proc.returncode==0,(proc.returncode,out,err)
   return proc.returncode
  finally:
   if proc.poll() is None:proc.kill();proc.communicate()
 for _ in range(3):
  codes.append(finish(start()))
  assert list(data.glob('.startup-*'))==[]
 proc=start()
 try:
  original=next(data.glob('.startup-*'));held=root/'held';original.rename(held)
  target=root/'keep';target.mkdir();(target/'marker').write_text('untouched')
  original.symlink_to(target,target_is_directory=True)
  finish(proc)
  assert original.is_symlink() and (target/'marker').read_text()=='untouched'
  assert (held/'startup-url.txt').exists()
 finally:
  if proc.poll() is None:proc.kill();proc.communicate()
 print(json.dumps({'sighup_exit_codes':codes,'three_clean_exits':True,'replacement_symlink_preserved':True,'historical_directories_not_scanned':True}))
