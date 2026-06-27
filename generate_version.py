#!/usr/bin/env python3
"""Run this script before deploying to generate version.py from git tags."""
import os

version = os.popen("git describe --tags --always --dirty").read().strip()

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "version.py")
with open(out, "w") as f:
    f.write(f'VERSION = "{version}"\n')
print(f"Written: {out}  ({version})")
