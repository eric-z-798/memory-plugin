"""Run the exact committed tree under /tmp without loosening production rules."""
import io
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
archive=subprocess.check_output(['git','archive',sha],cwd=root)
with tempfile.TemporaryDirectory(prefix='xingshu-checkout-') as tmp:
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        # Entries are from our own fixed Git tree, not an external archive.
        bundle.extractall(tmp)
    print('EXACT_SHA',sha,'CHECKOUT',tmp,flush=True)
    result=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests'],cwd=tmp)
    raise SystemExit(result.returncode)
