#!/usr/bin/env python3
"""Upload collector JSONL logs to the hometemp server (backfill / interim uploader until falltemp.3.3).

Usage: python3 collector/tools/upload_jsonl.py [--url URL] [--token-file PATH] [log dir or files...]
Defaults: https://hometemp.fallday.ca, token from ~/Library/Application Support/HomeTemp/token,
logs from ~/Library/Application Support/HomeTemp/events-*.jsonl. The server is idempotent, so re-running is safe.
"""
import argparse
import json
import pathlib
import urllib.request

HOME = pathlib.Path("~/Library/Application Support/HomeTemp").expanduser()

p = argparse.ArgumentParser()
p.add_argument("--url", default="https://hometemp.fallday.ca")
p.add_argument("--token-file", default=HOME / "token", type=pathlib.Path)
p.add_argument("--batch", type=int, default=2000)
p.add_argument("paths", nargs="*", type=pathlib.Path, default=[HOME])
args = p.parse_args()

token = args.token_file.read_text().strip()
files = [f for path in args.paths for f in (sorted(path.glob("events-*.jsonl")) if path.is_dir() else [path])]
for f in files:
    records = [json.loads(line) for line in f.open() if line.strip()]
    for i in range(0, len(records), args.batch):
        body = json.dumps({"records": records[i:i + args.batch]}).encode()
        req = urllib.request.Request(f"{args.url}/api/ingest", data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            print(f.name, i, json.loads(r.read()))
