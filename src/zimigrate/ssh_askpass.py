"""OpenSSH ASKPASS helper. Prints a password file to stdout; never logs it."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    path = os.environ.get("ZIMIGRATE_SSH_ASKPASS_FILE", "")
    if not path:
        return 1
    try:
        sys.stdout.write(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
