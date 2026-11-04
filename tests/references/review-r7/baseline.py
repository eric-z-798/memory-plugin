import os,sys,tempfile,subprocess,socket,time,json
from pathlib import Path
from unittest.mock import patch
ROOT=Path('/workspace/memory-plugin');sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import secure_fixtures
from aigc.security import PrivateStartupFiles
with tempfile.TemporaryDirectory(dir='/workspace') as home, tempfile.TemporaryDirectory() as checkout:
 with patch.dict(os.environ,{'HOME':home}), patch.object(secure_fixtures,'__file__',str(Path(checkout)/'tests/secure_fixtures.py')), patch.object(secure_fixtures,'Path') as mocked:
  # Replace only /workspace candidate; preserve pathlib behavior otherwise.
  mocked.side_effect=lambda value: Path(home)/'missing-workspace' if value=='/workspace' else Path(value)
  mocked.home.return_value=Path(home)
  try:
   with secure_fixtures.secure_temporary_directory() as result:print('HOME_RESULT',result)
  except RuntimeError as exc:print('HOME_FAILURE',str(exc))
with tempfile.TemporaryDirectory(dir='/workspace') as tmp:
 data=Path(tmp)/'data'; codes=[]
 for i in range(3):
  with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
  proc=subprocess.Popen([sys.executable,'run.py','--demo','--no-browser','--port',str(port),'--data-dir',str(data)],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  try:
   for _ in range(100):
    with socket.socket() as sock:
     if sock.connect_ex(('127.0.0.1',port))==0:break
    time.sleep(.05)
   time.sleep(.1);proc.send_signal(__import__('signal').SIGHUP);proc.communicate(timeout=5);codes.append(proc.returncode)
  finally:
   if proc.poll() is None:proc.kill();proc.communicate()
 print('SIGHUP_CODES',codes,'STARTUP_RESIDUES',len(list(data.glob('.startup-*'))))
