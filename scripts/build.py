#!/usr/bin/env python3
"""Build a self-contained ServerWatch zipapp using only Python's standard library."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys
import tempfile
import zipapp


def main() -> int:
    if sys.version_info < (3, 10):
        raise SystemExit("ServerWatch requires Python 3.10 or newer")
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=root / "dist" / "serverwatch.pyz")
    args = parser.parse_args()
    output = args.output.expanduser().absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="serverwatch-build-") as temporary:
        source = Path(temporary) / "app"
        source.mkdir()
        shutil.copytree(
            root / "serverwatch", source / "serverwatch",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        (source / "__main__.py").write_text(
            "from serverwatch.cli import main\nraise SystemExit(main())\n", encoding="utf-8",
        )
        with tempfile.NamedTemporaryFile(
            prefix=".serverwatch-", suffix=".pyz", dir=output.parent, delete=False,
        ) as handle:
            pending = Path(handle.name)
        try:
            zipapp.create_archive(source, pending, interpreter="/usr/bin/env python3")
            pending.chmod(0o755)
            os.replace(pending, output)
        finally:
            pending.unlink(missing_ok=True)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
