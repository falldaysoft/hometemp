#!/usr/bin/env python3
"""Summarize collector spike logs (falltemp.1).

Usage: python3 collector/tools/spike_report.py [log dir]
Default log dir: ~/Library/Application Support/HomeTemp

Reports, per characteristic: event count, median/max gap between events, and the
largest gap in *any* record (event or heartbeat) — a gap much larger than the
5-minute heartbeat means the collector stopped running (App Nap, sleep, crash).
"""
import collections
import datetime as dt
import json
import pathlib
import re
import statistics
import sys

d = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "~/Library/Application Support/HomeTemp").expanduser()
rows = [json.loads(line) for f in sorted(d.glob("events-*.jsonl")) for line in f.open()]
# Ignore records from builds before characteristic names were mapped (raw HomeKit UUIDs).
rows = [r for r in rows if not str(r.get("characteristic", "")).startswith("000000")]
ts = lambda r: dt.datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))

if not rows:
    sys.exit(f"no logs in {d}")

start, end = ts(rows[0]), ts(rows[-1])
print(f"Log span: {start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} UTC ({(end - start).total_seconds() / 3600:.1f} h), {len(rows)} records")

# Collector liveness: biggest gap between consecutive records of any kind.
times = sorted(ts(r) for r in rows)
gaps = sorted(((b - a).total_seconds(), a) for a, b in zip(times, times[1:]))[-5:]
print("\nLargest gaps between any records (collector liveness; heartbeat is 300 s):")
for g, at in reversed(gaps):
    flag = "  <-- collector likely paused" if g > 400 else ""
    print(f"  {g / 60:7.1f} min starting {at:%m-%d %H:%M}{flag}")

# Label gaps with macOS sleep/wake and app lifecycle so they aren't blamed on the collector.
import subprocess
try:
    pm = subprocess.run(["pmset", "-g", "log"], capture_output=True, text=True, timeout=60).stdout.splitlines()
    sleeps = [l for l in pm if re.search(r"\b(Sleep|Wake|DarkWake)\s{2,}", l)]
except Exception:
    sleeps = []
def local(t): return t.astimezone()
span = [l for l in sleeps if l[:19] >= f"{local(start):%Y-%m-%d %H:%M:%S}"]
if span:
    print("\nmacOS sleep/wake during the run (pmset):")
    for l in span[-40:]:
        print("  " + l[:110])
life = [r for r in rows if r.get("type") in ("lifecycle", "launch")]
if life:
    print("\nApp lifecycle:")
    for r in life:
        print(f"  {local(ts(r)):%m-%d %H:%M:%S} {r.get('event', 'launch')}")
    # Events received while backgrounded = the answer to "does it work minimized/hidden/closed?"
    bg, state = 0, "fg"
    for r in rows:
        if r.get("type") == "lifecycle":
            state = "bg" if r["event"] in ("didEnterBackground", "sceneDidDisconnect") else ("fg" if r["event"] in ("willEnterForeground", "sceneDidActivate", "didBecomeActive") else state)
        elif r.get("type") == "value" and r["source"] == "event" and state == "bg":
            bg += 1
    print(f"  HomeKit events received while backgrounded: {bg}")

events = collections.defaultdict(list)
last = {}
for r in rows:
    if r.get("type") != "value":
        continue
    key = (r["room"], r["accessory"], r["characteristic"])
    last[key] = r["value"]
    if r["source"] == "event":
        events[key].append(ts(r))

print("\nEvent cadence per characteristic:")
print(f"  {'room':<12} {'accessory':<28} {'characteristic':<26} {'n':>5} {'med gap':>8} {'max gap':>8}  last")
for key in sorted(last):
    ev = events.get(key, [])
    deltas = [(b - a).total_seconds() / 60 for a, b in zip(ev, ev[1:])]
    med = f"{statistics.median(deltas):.1f}m" if deltas else "—"
    mx = f"{max(deltas):.0f}m" if deltas else "—"
    print(f"  {key[0][:12]:<12} {key[1][:28]:<28} {key[2][:26]:<26} {len(ev):>5} {med:>8} {mx:>8}  {last[key]}")

# Thermostat run-time from CurrentHeaterCoolerState (2 heating, 3 cooling) or CurrentHeatingCooling (1 heat, 2 cool).
print("\nHVAC state transitions:")
for r in rows:
    if r.get("type") == "value" and r["characteristic"] in ("currentHeaterCoolerState", "currentHeatingCooling") and r["source"] != "heartbeat":
        print(f"  {ts(r):%m-%d %H:%M:%S} {r['accessory']}: {r['characteristic']}={r['value']} ({r['source']})")

errs = collections.Counter((r["accessory"], r.get("op"), r.get("error")) for r in rows if r.get("type") == "error")
if errs:
    print("\nErrors:")
    for (a, op, e), n in errs.items():
        print(f"  {n}× {a}: {op}: {e}")
reach = [r for r in rows if r.get("type") == "reachability"]
if reach:
    print("\nReachability changes:")
    for r in reach:
        print(f"  {ts(r):%m-%d %H:%M} {r['accessory']}: reachable={r['reachable']}")
