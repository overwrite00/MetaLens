"""Build the registered samples for manual inspection.

Usage (from ``python/``): ``python -m tests.factories --out <dir> [name ...]``
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import ALL


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.factories")
    parser.add_argument("--out", required=True, type=Path, help="output directory")
    parser.add_argument("names", nargs="*", help="sample names (default: all)")
    args = parser.parse_args(argv)
    known = {s.name: s for s in ALL}
    unknown = [n for n in args.names if n not in known]
    if unknown:
        parser.error(f"unknown samples: {', '.join(unknown)}")
    selected = [known[n] for n in args.names] if args.names else ALL
    if not selected:
        print("No samples registered.")
    for s in selected:
        if not s.available:
            print(f"{s.name}: skipped on {sys.platform}")
            continue
        print(f"{s.name}: {s.make(args.out / s.name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
