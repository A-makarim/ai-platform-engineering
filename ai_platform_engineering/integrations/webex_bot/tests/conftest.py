# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""pytest bootstrap for the webex_bot test suite.

Expose the integration package as ``webex_bot`` so imports match the
Docker runtime layout.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parents[1]  # .../webex_bot/
_INTEGRATIONS_DIR = _PKG_DIR.parent             # .../integrations/

candidate = str(_INTEGRATIONS_DIR)
if candidate not in sys.path:
    sys.path.insert(0, candidate)

# Keep flat and fully-qualified imports on the same module object.
if "webex_bot" not in sys.modules:
    pkg = types.ModuleType("webex_bot")
    pkg.__path__ = [str(_PKG_DIR)]  # behave like a regular package
    sys.modules["webex_bot"] = pkg
