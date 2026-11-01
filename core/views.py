from collections import defaultdict
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import OuterRef, Subquery
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from .models import Accessory, Characteristic, CollectorDevice, Reading, Zone

STALE_AFTER = timedelta(minutes=15)
HEATER_COOLER_STATE = {2: "heating", 3: "cooling"}  # 0 inactive / 1 idle → off
HEATING_COOLING_STATE = {1: "heating", 2: "cooling"}
TARGET_HEATER_COOLER = {0: "Auto", 1: "Heat", 2: "Cool"}
TARGET_HEATING_COOLING = {0: "Off", 1: "Heat", 2: "Cool", 3: "Auto"}


def health(request):
    # The container healthcheck in deploy/docker-compose.yml hits this.
    return HttpResponse("ok")


def latest_values():
    """{accessory_id: {kind: (value, ts)}} from the newest reading of each characteristic."""
    newest = Reading.objects.filter(characteristic=OuterRef("pk")).order_by("-ts")
    rows = Characteristic.objects.annotate(
        v=Subquery(newest.values("value")[:1]), t=Subquery(newest.values("ts")[:1])
    ).values("accessory_id", "kind", "v", "t")
    out = defaultdict(dict)
    for r in rows:
        if r["v"] is not None:
            out[r["accessory_id"]][r["kind"]] = (r["v"], r["t"])
    return out


def describe(acc, values, now):
    vals = values.get(acc.id, {})
    get = lambda k: vals.get(k, (None, None))[0]
    temp = get("currentTemperature")
    if temp is not None:
        temp += acc.calibration_offset
    seen = max((t for _, t in vals.values()), default=None)
    return {
        "acc": acc,
        "temp": temp,
        "humidity": get("currentRelativeHumidity"),
        "co2": get("carbonDioxideLevel"),
        "battery": get("batteryLevel"),
        "seen": seen,
        "stale": seen is None or now - seen > STALE_AFTER or not acc.reachable,
        "kinds": set(vals),
    }


def thermostat_state(d, values):
    vals = values.get(d["acc"].id, {})
    get = lambda k: vals.get(k, (None, None))[0]
    if "currentHeaterCoolerState" in vals:
        state = HEATER_COOLER_STATE.get(int(get("currentHeaterCoolerState")), "idle")
        mode = TARGET_HEATER_COOLER.get(int(get("targetHeaterCoolerState") or 0), "?")
        if get("active") == 0:
            mode = "Off"
    else:
        state = HEATING_COOLING_STATE.get(int(get("currentHeatingCooling") or 0), "idle")
        mode = TARGET_HEATING_COOLING.get(int(get("targetHeatingCooling") or 0), "?")
    return {
        **d,
        "state": state,
        "mode": mode,
        "heat_to": get("heatingThreshold") if get("heatingThreshold") is not None else get("targetTemperature"),
        "cool_to": get("coolingThreshold"),
    }


def is_thermostat(d):
    return bool({"currentHeaterCoolerState", "currentHeatingCooling"} & d["kinds"])


@login_required
def index(request):
    now = timezone.now()
    values = latest_values()
    accessories = Accessory.objects.select_related("homekit_room__zone", "room_override__zone").exclude(
        role=Accessory.Role.IGNORE
    )
    described = [describe(a, values, now) for a in accessories]
    with_temp = [d for d in described if d["temp"] is not None or is_thermostat(d)]

    zones = []
    for zone in Zone.objects.all():
        members = [d for d in with_temp if d["acc"].zone == zone and d["acc"].role == Accessory.Role.AMBIENT]
        thermo = next((d for d in members if zone.thermostat_id == d["acc"].id), None) or next(
            (d for d in members if is_thermostat(d)), None
        )
        thermo = thermostat_state(thermo, values) if thermo else None
        rooms = [d for d in members if not thermo or d["acc"].id != thermo["acc"].id]
        for d in rooms:
            d["delta"] = d["temp"] - thermo["temp"] if thermo and d["temp"] is not None and thermo["temp"] is not None else None
        zones.append({"zone": zone, "thermostat": thermo, "rooms": rooms})

    collector = CollectorDevice.objects.order_by("-last_record_ts").first()
    latest_reading = Reading.objects.order_by("-ts").values_list("ts", flat=True).first()
    context = {
        "zones": zones,
        "appliances": [d for d in with_temp if d["acc"].role == Accessory.Role.APPLIANCE],
        "outdoor": [d for d in with_temp if d["acc"].role == Accessory.Role.OUTDOOR],
        "review": [d for d in with_temp if d["acc"].role == Accessory.Role.UNCLASSIFIED],
        "collector": collector,
        "latest_reading": latest_reading,
        "collector_stale": latest_reading is None or now - latest_reading > STALE_AFTER,
        "now": now,
    }
    return render(request, "core/index.html", context)
