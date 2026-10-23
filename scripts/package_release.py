"""Package the repository into the release asset.

The asset is the *tracked* tree of the repository, so it always matches the
commit the tag points at - no stale files, no virtualenv, no build output.

    python scripts/package_release.py --version 1.0.0 --out dist/ensp-mcp-v1.0.0.zip
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", required=True, help="e.g. 1.0.0")
    ap.add_argument("--out", default="", help="zip path (default dist/ensp-mcp-v<version>.zip)")
    args = ap.parse_args()

    name = f"ensp-mcp-v{args.version}"
    out = Path(args.out) if args.out else ROOT / "dist" / f"{name}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)

    files = tracked_files()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            src = ROOT / rel
            if not src.is_file():
                print(f"skipping missing {rel}", file=sys.stderr)
                continue
            z.write(src, f"{name}/{rel.replace(chr(92), '/')}")

    print(f"wrote {out} ({out.stat().st_size} bytes, {len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
