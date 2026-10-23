"""Replace the asset of a GitHub release.

The token comes from the git credential helper (GCM) and is used in memory
only - it is never written to disk and never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"


def token_from_helper() -> str:
    query = "protocol=https\nhost=github.com\n\n"
    proc = subprocess.run(["git", "credential", "fill"], input=query,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"git credential fill failed: {proc.stderr.strip()}")
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no password in the credential helper output")


def request(method: str, url: str, token: str, data=None, headers=None):
    hdrs = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ensp-mcp-release",
    }
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--asset", required=True, help="file to upload")
    ap.add_argument("--name", required=True, help="asset name on the release")
    args = ap.parse_args()

    token = token_from_helper()
    if not token:
        raise SystemExit("no token")

    _, rel = request("GET", f"{API}/repos/{args.repo}/releases/tags/{args.tag}", token)
    if not isinstance(rel, dict):
        print("cannot read the release:", rel, file=sys.stderr)
        return 1
    print("release:", rel.get("name"), "| assets:",
          ", ".join(a["name"] for a in rel.get("assets", [])))

    for asset in rel.get("assets", []):
        if asset["name"] == args.name:
            status, _ = request("DELETE", asset["url"], token)
            print("deleted the old asset:", asset["name"], status)

    payload = open(args.asset, "rb").read()
    upload_url = rel.get("upload_url", "").split("{", 1)[0]
    if not upload_url:
        upload_url = f"{API}/repos/{args.repo}/releases/{rel['id']}/assets"
    sep = "&" if "?" in upload_url else "?"
    url = f"{upload_url}{sep}name={urllib.parse.quote(args.name)}"
    status, out = request(
        "POST",
        url,
        token,
        data=payload,
        headers={"Content-Type": "application/zip",
                 "Content-Length": str(len(payload))},
    )
    if status not in (201, 200):
        print("upload failed:", status, out, file=sys.stderr)
        return 1
    print("uploaded:", out.get("name"), out.get("size"), "bytes ->", out.get("browser_download_url"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
