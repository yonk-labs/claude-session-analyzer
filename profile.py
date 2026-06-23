#!/usr/bin/env python3
"""Thin shim: `python3 profile.py ...` == `claude-trace ...`.

Kept so the original entry point still works. Real code lives in claude_trace/.
"""
from claude_trace.cli import main

if __name__ == "__main__":
    main()
