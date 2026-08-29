"""Compatibility alias for retired fixed-stage Codex task builders."""

from __future__ import annotations

import sys
from importlib import import_module


_implementation = import_module("legacy.story_agent_v3.story_codex_tasks")
sys.modules[__name__] = _implementation
