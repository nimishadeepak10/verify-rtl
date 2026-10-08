"""Call api.main.formal_check directly with every option it has, filling in
the defaults FastAPI would have applied over HTTP.

Calling an endpoint function directly (as the scripts here do) leaves any
omitted `Form(...)` parameter as a `Form` object instead of its default
value, so every new option added to /api/formal used to break each script
that predated it. Importing `formal_check` from here instead keeps those
scripts working as options are added.
"""

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from api.main import formal_check as _formal_check  # noqa: E402


def formal_check(**kwargs):
    for name, param in inspect.signature(_formal_check).parameters.items():
        if name not in kwargs:
            default = param.default
            kwargs[name] = getattr(default, "default", default)
    return _formal_check(**kwargs)
