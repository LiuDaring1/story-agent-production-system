#!/usr/bin/env python3
"""Compatibility shim for the retired fixed-stage Story Agent.

New production must enter through a Codex task using ``story-full-auto``.  The
implementation lives in ``legacy/story_agent_v3`` only for historical imports,
audits, and migration tests.
"""

from __future__ import annotations

import sys
from importlib import import_module


_implementation = import_module("legacy.story_agent_v3.story_agent")

if __name__ == "__main__":
    print(
        "[LEGACY] story_agent.py 已退役；新生产请在 Codex 中使用 story-full-auto。",
        file=sys.stderr,
    )
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
