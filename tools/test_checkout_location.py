"""Run the exact committed tree under /tmp without loosening production rules."""
import io
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from qa_auth import private_data_directory
root=Path(__file__).resolve().parents[1]
sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
archive=subprocess.check_output(['git','archive',sha],cwd=root)
with tempfile.TemporaryDirectory(prefix='xingshu-checkout-') as tmp, private_data_directory() as home:
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        # Entries are from our own fixed Git tree, not an external archive.
        bundle.extractall(tmp)
    print('EXACT_SHA',sha,'CHECKOUT',tmp,flush=True)
    env = dict(os.environ, HOME=home)
    env.pop('XINGSHU_TEST_SECURE_ROOT', None)
    # Model a developer machine with no /workspace without touching that real
    # directory or weakening path validation. Only the fixture candidate changes.
    bootstrap = ("import sys,runpy;from pathlib import Path;sys.path.insert(0,'tests');"
                 "import secure_fixtures;secure_fixtures.WORKSPACE=Path(" + repr(str(Path(home)/'absent-workspace')) + ");"
                 "sys.argv=['unittest','discover','-s','tests'];runpy.run_module('unittest',run_name='__main__')")
    print('SIMULATED_HOME', home, 'WORKSPACE_CANDIDATE=absent', 'SECURE_ROOT_OVERRIDE=unset', flush=True)
    result=subprocess.run([sys.executable,'-c',bootstrap],cwd=tmp,env=env)
    raise SystemExit(result.returncode)
