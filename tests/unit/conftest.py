"""Load SmartHeat's pure-Python modules without importing Home Assistant.

Importing custom_components.smartheat would run its __init__.py, which needs Home
Assistant. Registering the folder as a bare package called smartheat_core lets the
tests import controller.py, tickle.py and learning.py directly.
"""

import pathlib
import sys
import types

PACKAGE = "smartheat_core"
SOURCE = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "smartheat"

if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(SOURCE)]
    sys.modules[PACKAGE] = package
