"""
Reports which version is actually running on this server.

deploy.sh writes a VERSION file (git tag + short commit SHA + deploy
timestamp) every time it deploys. This just reads that file so you can
glance at /admin and confirm the server is running what you think it's
running — handy for catching a deploy that silently failed partway.
"""

import os

VERSION_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "VERSION")


def get_version() -> str:
    if os.path.exists(VERSION_FILE):
        with open(VERSION_FILE) as f:
            return f.read().strip()
    return "dev (no VERSION file — not deployed via deploy.sh)"
