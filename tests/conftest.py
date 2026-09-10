"""Load the Home-Assistant-free modules without a Home Assistant install.

Importing it through the package would run ``custom_components/ugreen_connect/__init__.py``,
which pulls in Home Assistant. The session logic is deliberately free of those imports, so
the module is compiled straight from its source text -- which also fails loudly the moment
someone adds a Home Assistant import to it.

The source is compiled here rather than imported so that ``__pycache__`` is never
consulted: two edits a second apart that leave the file the same length look unchanged
to the bytecode cache, and the tests would then run against the previous version.
"""

import importlib.util
import sys
from pathlib import Path

_COMPONENT = (
    Path(__file__).resolve().parents[1] / "custom_components" / "ugreen_connect"
)


def _load(name: str, file_name: str):
    path = _COMPONENT / file_name
    spec = importlib.util.spec_from_loader(name, loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
    return module


session = _load("ugreen_session", "session.py")
protocol = _load("ugreen_protocol", "protocol.py")
