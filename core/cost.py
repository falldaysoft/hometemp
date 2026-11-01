"""Run time → energy → cost. Estimates: HomeKit reports on/off only (no stage, no blower)."""
from collections import defaultdict
from datetime import timedelta

from django.utils import timezone

from .models import HomeSettings, HvacInterval, WeatherHour, Zone

BTU_PER_M3 = 35_300  # natural gas, ~37.3 MJ/m³


def zone_thermostats():
    """{thermostat accessory id: zone} using each zone's thermostat or its auto-detected one."""
    from .hvac import thermostats
    out = {}
    for acc in thermostats().select_related("homekit_room__zone", "room_override__zone"):
        zone = next((z for z in Zone.objects.filter(thermostat=acc)), None) or acc.zone
        if zone:
            out[acc.id] = zone
    return out


def run_cost(run_minutes, mode, zone, settings):
    h = run_minutes / 60
    if mode == "heating":
        m3 = zone.furnace_input_btuh * h / BTU_PER_M3
        kwh = zone.blower_kw * h
        return {"m3": m3, "kwh": kwh, "cost": m3 * settings.gas_price_m3 + kwh * settings.electricity_kwh}
    kwh = zone.ac_kw * h
    return {"m3": 0.0, "kwh": kwh, "cost": kwh * settings.electricity_kwh}


def daily(days=14, now=None):
    """[{date, zones: {zone_id: {heat_min, cool_min, m3, kwh, cost}}, total, outdoor_mean}] oldest first."""
    now = now or timezone.now()
    settings = HomeSettings.load()
    tz_now = timezone.localtime(now)
    start = (tz_now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    zt = zone_thermostats()
    zones = {z.id: z for z in Zone.objects.all()}
    days_out = []
    for i in range(days):
        d0 = start + timedelta(days=i)
        days_out.append({"date": d0.date(), "start": d0, "end": d0 + timedelta(days=1),
                         "zones": defaultdict(lambda: {"heat_min": 0.0, "cool_min": 0.0, "m3": 0.0, "kwh": 0.0, "cost": 0.0})})
    for run in HvacInterval.objects.filter(end__gte=start, thermostat_id__in=zt):
        zone = zt[run.thermostat_id]
        for d in days_out:
            a, b = max(run.start, d["start"]), min(run.end, d["end"], now)
            if b <= a:
                continue
                
            minutes = (b - a).total_seconds() / 60
            z = d["zones"][zone.id]
            z["heat_min" if run.mode == "heating" else "cool_min"] += minutes
            for k, v in run_cost(minutes, run.mode, zone, settings).items():
                z[k] += v
    for d in days_out:
        temps = list(WeatherHour.objects.filter(issued=None, ts__gte=d["start"], ts__lt=d["end"])
                     .values_list("temperature", flat=True))
        temps = [t for t in temps if t is not None]
        d["outdoor_mean"] = sum(temps) / len(temps) if temps else None
        d["hdd"] = max(0.0, 18 - d["outdoor_mean"]) if d["outdoor_mean"] is not None else None
        d["total"] = sum(z["cost"] for z in d["zones"].values())
        d["zones"] = dict(d["zones"])
    return {"days": days_out, "zones": zones, "settings": settings,
            "estimated": not settings.prices_confirmed or any(not z.equipment_confirmed for z in zones.values()
                                                               if z.id in {v.id for v in zt.values()})}
