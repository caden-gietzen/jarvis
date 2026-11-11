r"""What jarvis.cmd runs (`.\jarvis <command>`): `python -m jarvis` on the base interpreter, using the venv's packages."""

import runpy
import site
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
site.addsitedir(str(ROOT / ".venv" / "Lib" / "site-packages"))  # also processes the .pth files
sys.path.insert(0, str(ROOT))
runpy.run_module("jarvis", run_name="__main__", alter_sys=True)
