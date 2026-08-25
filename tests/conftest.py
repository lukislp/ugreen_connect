"""Load ``session`` without a Home Assistant install.

Importing it through the package would run ``custom_components/ugreen_connect/__init__.py``,
which pulls in Home Assistant. The session logic is deliberately free of those imports, so
the module is loaded straight from its file instead -- which also fails loudly the moment
someone adds a Home Assistant import to it.
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

_spec = importlib.util.spec_from_file_location("ugreen_session", _PATH)
session = importlib.util.module_from_spec(_spec)
sys.modules["ugreen_session"] = session
_spec.loader.exec_module(session)
