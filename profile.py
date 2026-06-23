#!/usr/bin/env python3
"""Thin shim: `python3 profile.py ...` == `csa ...`.

Kept so the original entry point still works. Real code lives in csa/.
"""
from csa.cli import main

if __name__ == "__main__":
    main()
