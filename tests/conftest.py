"""Load ``session`` without a Home Assistant install.

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

_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "ugreen_connect"
    / "session.py"
)

_spec = importlib.util.spec_from_loader("ugreen_session", loader=None)
session = importlib.util.module_from_spec(_spec)
session.__file__ = str(_PATH)
sys.modules["ugreen_session"] = session
exec(compile(_PATH.read_text(), str(_PATH), "exec"), session.__dict__)
