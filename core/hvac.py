"""Derive furnace/AC run intervals from thermostat state readings.

Handles both HomeKit shapes: Thermostat (CurrentHeatingCooling 1 heat, 2 cool) and HeaterCooler
(CurrentHeaterCoolerState 2 heating, 3 cooling; the Resideo X2S reports 0 when idle).
- Blips shorter than DEBOUNCE are dropped (a setpoint change produced 0→2→0 within 0.7 s).
- If the collector goes quiet for more than GAP during a run, the run is closed at the last reading
  and marked uncertain rather than guessed.
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import Accessory, HvacInterval, Reading

DEBOUNCE = timedelta(seconds=60)
GAP = timedelta(minutes=15)
STATE_KINDS = {"currentHeaterCoolerState": {2: "heating", 3: "cooling"},
               "currentHeatingCooling": {1: "heating", 2: "cooling"}}


def thermostats():
    return Accessory.objects.filter(characteristics__kind__in=STATE_KINDS).distinct()


def intervals(acc, since=None, until=None):
    ch = acc.characteristics.filter(kind__in=STATE_KINDS).first()
    if ch is None:
        return []
    mapping = STATE_KINDS[ch.kind]
    qs = Reading.objects.filter(characteristic=ch).order_by("ts")
    if since:
        # Start from the last reading before the window so a run already in progress is caught.
        prior = qs.filter(ts__lt=since).last()
        qs = qs.filter(ts__gte=prior.ts if prior else since)
    if until:
        qs = qs.filter(ts__lte=until)
    out, cur, last_ts = [], None, None
    for ts, v in qs.values_list("ts", "value"):
        if cur and last_ts and ts - last_ts > GAP:
            out.append({**cur, "end": last_ts, "uncertain": True})
            cur = None
        mode = mapping.get(int(v))
        if cur and cur["mode"] != mode:
            out.append({**cur, "end": ts})
            cur = None
        if mode and cur is None:
            cur = {"start": ts, "mode": mode, "uncertain": False}
        last_ts = ts
    if cur:
        now = until or timezone.now()
        if now - last_ts > GAP:
            out.append({**cur, "end": last_ts, "uncertain": True})
        else:
            out.append({**cur, "end": now, "open": True})
    return [i for i in out if i["end"] - i["start"] >= DEBOUNCE or i.get("open") or i.get("uncertain")]


@transaction.atomic
def rebuild(acc, since=None):
    """Replace stored runs for this thermostat from `since` (or all of history) onward."""
    runs = intervals(acc, since=since)
    qs = HvacInterval.objects.filter(thermostat=acc)
    if since and runs:
        qs = qs.filter(end__gte=min(since, runs[0]["start"]))
    elif since:
        qs = qs.filter(end__gte=since)
    qs.delete()
    HvacInterval.objects.bulk_create([
        HvacInterval(thermostat=acc, start=r["start"], end=r["end"], mode=r["mode"],
                     open=r.get("open", False), uncertain=r.get("uncertain", False)) for r in runs])
    return len(runs)


def refresh_recent(hours=48):
    since = timezone.now() - timedelta(hours=hours)
    return {acc.name: rebuild(acc, since) for acc in thermostats()}
