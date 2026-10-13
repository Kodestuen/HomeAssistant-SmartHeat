"""Shared test setup.

The Home Assistant tests need pytest-homeassistant-custom-component, which needs the
same Python as Home Assistant itself (3.14). Without it only the unit tests run.
"""

import importlib.util

collect_ignore = []
if importlib.util.find_spec("pytest_homeassistant_custom_component") is None:
    collect_ignore.append("ha")
