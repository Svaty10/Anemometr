# Loaded by Python at start-up of the tank hub (retencka) only: the vitr installer puts
# this directory on PYTHONPATH in /etc/systemd/system/retencka.service.d/vitr-most.conf.
# It attaches the anemometer radio bridge (vitr_most.py) and then runs the system's own
# sitecustomize, if there is one, so nothing else changes.
import importlib.machinery as _machinery
import importlib.util as _util
import os as _os
import sys as _sys

_here = _os.path.dirname(_os.path.abspath(__file__))
try:
    import vitr_most as _vitr_most
    _vitr_most.install()
except Exception:
    pass

try:
    _spec = _machinery.PathFinder.find_spec(
        "sitecustomize", [p for p in _sys.path if _os.path.abspath(p or ".") != _here])
    if _spec is not None and _spec.loader is not None:
        _spec.loader.exec_module(_util.module_from_spec(_spec))
except Exception:
    pass
