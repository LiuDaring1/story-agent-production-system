"""Compatibility alias for the retired Story Agent supervisor."""

from __future__ import annotations

import sys
from importlib import import_module


_implementation = import_module("legacy.story_agent_v3.story_agent_supervisor")
sys.modules[__name__] = _implementation
