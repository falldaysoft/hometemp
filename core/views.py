from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from . import weather
from .cost import daily
from .dashboard import accessory_detail, dashboard, outdoor_detail, timeline
from .models import Accessory, HomeSettings, OccupancyException, Room, Zone, ZoneSchedule
from . import occupancy


def health(request):
    # The container healthcheck in deploy/docker-compose.yml hits this.
    return HttpResponse("ok")


@login_required
def index(request):
    template = "core/_dashboard.html" if request.headers.get("HX-Request") else "core/index.html"
    return render(request, template, dashboard())


def _sheet(request, acc, saved=False, status=200):
    hours = 168 if request.GET.get("range") == "week" else 24
    show = [k for k in request.GET.get("show", "").split(",") if k]
    detail = accessory_detail(acc, hours=hours, show=show)
    rng = "week" if hours != 24 else "day"
    for c in detail["overlay_choices"]:
        toggled = [k for k in detail["show"] if k != c["kind"]] + ([] if c["on"] else [c["kind"]])
        c["query"] = f"?range={rng}&show={','.join(toggled)}"
    detail["show_param"] = ",".join(detail["show"])
    ctx = {**detail, "rooms": Room.objects.select_related("zone").order_by("zone__order", "name"),
           "roles": [r for r in Accessory.Role if r != Accessory.Role.UNCLASSIFIED], "saved": saved}
    resp = render(request, "core/_sheet_accessory.html", ctx, status=status)
    if saved:
        resp["HX-Trigger"] = "dashboard-changed"
    return resp


@login_required
def accessory(request, pk):
    return _sheet(request, get_object_or_404(Accessory, pk=pk))


@login_required
@require_POST
def accessory_update(request, pk):
    acc = get_object_or_404(Accessory, pk=pk)
    role = request.POST.get("role")
    if role in Accessory.Role.values:
        acc.role = role
    if "room" in request.POST:
        room_id = request.POST["room"]
        acc.room_override = None if room_id in ("", "homekit") else get_object_or_404(Room, pk=room_id)
        if acc.room_override_id == acc.homekit_room_id:
            acc.room_override = None
    if "offset" in request.POST:
        try:
            acc.calibration_offset = max(-10.0, min(10.0, round(float(request.POST["offset"]), 1)))
        except ValueError:
            pass
    acc.save()
    return _sheet(request, acc, saved=True)


@login_required
def rooms(request):
    if request.method == "POST":
        room = get_object_or_404(Room, pk=request.POST.get("room"))
        zone_id = request.POST.get("zone")
        room.zone = Zone.objects.filter(pk=zone_id).first() if zone_id else None
        room.save(update_fields=["zone"])
    ctx = {"rooms": Room.objects.select_related("zone").order_by("name"), "zones": Zone.objects.all(),
           "counts": {r.id: 0 for r in Room.objects.all()}}
    for acc in Accessory.objects.exclude(role=Accessory.Role.IGNORE).select_related("homekit_room", "room_override"):
        if acc.room:
            ctx["counts"][acc.room.id] = ctx["counts"].get(acc.room.id, 0) + 1
    resp = render(request, "core/_sheet_rooms.html", ctx)
    if request.method == "POST":
        resp["HX-Trigger"] = "dashboard-changed"
    return resp


@login_required
def outdoor(request):
    hours = 168 if request.GET.get("range") == "week" else 24
    return render(request, "core/_sheet_outdoor.html", outdoor_detail(hours))


@login_required
def settings_sheet(request):
    s = HomeSettings.load()
    saved = False
    if request.method == "POST":
        try:
            s.latitude = float(request.POST["latitude"])
            s.longitude = float(request.POST["longitude"])
        except (KeyError, ValueError):
            pass
        else:
            s.place = request.POST.get("place", "")[:200]
            s.timezone = request.POST.get("timezone", "")[:60]
            s.location_changed = True  # the scheduler container backfills within a minute
            s.weather_error = ""
            s.save()
            saved = True
    resp = render(request, "core/_sheet_settings.html", settings_context(s, saved))
    if saved:
        resp["HX-Trigger"] = "dashboard-changed"
    return resp


@login_required
def place_search(request):
    try:
        results = weather.geocode(request.GET.get("q", ""))
        error = None
    except Exception:
        results, error = [], "Couldn't reach the place search. Try again in a moment."
    return render(request, "core/_place_results.html", {"results": results, "error": error})


def settings_context(s, saved=False, saved_what=""):
    return {"s": s, "saved": saved, "saved_what": saved_what, "zones": Zone.objects.all()}


def _float(request, name, lo, hi):
    try:
        return max(lo, min(hi, float(request.POST[name])))
    except (KeyError, ValueError):
        return None


@login_required
@require_POST
def save_prices(request):
    s = HomeSettings.load()
    gas, elec = _float(request, "gas", 0, 10), _float(request, "electricity", 0, 5)
    if gas is not None and elec is not None:
        s.gas_price_m3, s.electricity_kwh, s.prices_confirmed = gas, elec, True
        s.save()
    resp = render(request, "core/_sheet_settings.html", settings_context(s, True, "prices"))
    resp["HX-Trigger"] = "dashboard-changed"
    return resp


@login_required
@require_POST
def save_equipment(request, pk):
    z = get_object_or_404(Zone, pk=pk)
    vals = {"furnace_input_btuh": _float(request, "btuh", 10000, 250000), "furnace_afue": _float(request, "afue", 50, 99),
            "blower_kw": _float(request, "blower", 0, 3), "ac_kw": _float(request, "ac", 0, 15)}
    if None not in vals.values():
        z.furnace_input_btuh = int(vals["furnace_input_btuh"])
        z.furnace_afue = vals["furnace_afue"] / 100
        z.blower_kw, z.ac_kw, z.equipment_confirmed = vals["blower_kw"], vals["ac_kw"], True
        z.save()
    resp = render(request, "core/_sheet_settings.html", settings_context(HomeSettings.load(), True, f"zone-{z.pk}"))
    resp["HX-Trigger"] = "dashboard-changed"
    return resp


@login_required
def cost(request):
    days = 30 if request.GET.get("range") == "month" else 14
    data = daily(days)
    peak = max((d["total"] for d in data["days"]), default=0) or 1
    for d in data["days"]:
        d["bars"] = [{"zone": data["zones"][zid], "cost": z["cost"], "pct": 100 * z["cost"] / peak}
                     for zid, z in sorted(d["zones"].items())]
        d["heat_min"] = sum(z["heat_min"] for z in d["zones"].values())
        d["cool_min"] = sum(z["cool_min"] for z in d["zones"].values())
    totals = {
        "cost": sum(d["total"] for d in data["days"]),
        "m3": sum(z["m3"] for d in data["days"] for z in d["zones"].values()),
        "kwh": sum(z["kwh"] for d in data["days"] for z in d["zones"].values()),
        "heat_h": sum(d["heat_min"] for d in data["days"]) / 60,
    }
    return render(request, "core/_sheet_cost.html", {**data, "range_days": days, "totals": totals,
                                                     "rows": list(reversed(data["days"]))})


@login_required
def history(request):
    hours = {"day": 24, "3days": 72, "week": 168}.get(request.GET.get("range"), 24)
    zone = Zone.objects.filter(pk=request.GET.get("zone")).first() if request.GET.get("zone") else None
    return render(request, "core/_sheet_history.html", {
        "chart": timeline(zone, hours), "zones": Zone.objects.all(), "zone": zone, "hours": hours,
        "range": request.GET.get("range", "day")})


def _schedule_ctx(zone, message=""):
    from datetime import timedelta
    from django.utils import timezone
    now = timezone.now()
    sched = getattr(zone, "schedule", None)
    upcoming = []
    if sched:
        for d, name in sorted(occupancy.holiday_names(timezone.localtime(now).date(),
                                                      timezone.localtime(now).date() + timedelta(days=90)).items()):
            if d.weekday() in sched.days:
                upcoming.append({"start": d, "label": name, "kind": "holiday"})
    exceptions = OccupancyException.objects.filter(zone=zone, end__gte=now - timedelta(days=30))
    settings = HomeSettings.load()
    return {"zone": zone, "sched": sched, "state": occupancy.now_state(zone, now), "holidays": upcoming,
            "exceptions": exceptions, "evidence": occupancy.evidence(zone, 14, now), "message": message,
            "weekday_names": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], "settings": settings,
            "today": timezone.localtime(now).date()}


@login_required
def schedule(request, pk):
    zone = get_object_or_404(Zone, pk=pk)
    message = ""
    if request.method == "POST":
        action = request.POST.get("action")
        from datetime import datetime, time, timedelta
        from django.utils import timezone
        if action == "schedule":
            days = ",".join(sorted(d for d in request.POST.getlist("weekday") if d in "0123456"))
            try:
                start = time.fromisoformat(request.POST["start"])
                end = time.fromisoformat(request.POST["end"])
            except (KeyError, ValueError):
                start = end = None
            if request.POST.get("enabled") != "on":
                ZoneSchedule.objects.filter(zone=zone).delete()
                message = "Schedule removed: this side counts as always in use."
            elif start and end and end > start:
                ZoneSchedule.objects.update_or_create(zone=zone, defaults={
                    "weekdays": days, "start": start, "end": end,
                    "skip_holidays": request.POST.get("skip_holidays") == "on"})
                s = HomeSettings.load()
                s.holiday_country = request.POST.get("country", s.holiday_country)[:2].upper()
                s.holiday_subdivision = request.POST.get("subdivision", s.holiday_subdivision)[:10].upper()
                s.save(update_fields=["holiday_country", "holiday_subdivision"])
                message = "Schedule saved"
            else:
                message = "The end time has to be after the start time."
        elif action == "add":
            try:
                d0 = datetime.fromisoformat(request.POST["start"]).date()
                d1 = datetime.fromisoformat(request.POST.get("end") or request.POST["start"]).date()
            except (KeyError, ValueError):
                d0 = d1 = None
            kind = request.POST.get("kind")
            if d0 and d1 and d1 >= d0 and kind in OccupancyException.Kind.values:
                OccupancyException.objects.create(
                    zone=zone, kind=kind, note=request.POST.get("note", "")[:200],
                    start=timezone.make_aware(datetime.combine(d0, time(0))),
                    end=timezone.make_aware(datetime.combine(d1 + timedelta(days=1), time(0))))
                message = "Added"
            else:
                message = "Pick a start date, and an end date on or after it."
        elif action == "delete":
            OccupancyException.objects.filter(zone=zone, pk=request.POST.get("id")).delete()
            message = "Removed"
        zone = Zone.objects.get(pk=zone.pk)
    resp = render(request, "core/_sheet_schedule.html", _schedule_ctx(zone, message))
    if request.method == "POST":
        resp["HX-Trigger"] = "dashboard-changed"
    return resp


@login_required
def warmup(request, pk):
    from . import thermal
    zone = get_object_or_404(Zone, pk=pk)
    thermo, rooms = thermal.zone_devices(zone)
    calc = None
    if request.GET.get("calc"):
        try:
            f, t, o = (float(request.GET[k]) for k in ("from", "to", "outdoor"))
            calc = {"from": f, "to": t, "outdoor": o, **thermal.eta(zone, f, t, o)}
        except (KeyError, ValueError):
            calc = {"error": "Enter three temperatures."}
    ctx = {"zone": zone, "thermo": thermo, "warmups": list(reversed(thermal.warmups(zone))),
           "coasts": [c for c in reversed(thermal.coasts(zone)) if c["hours"] >= 2],
           "model": thermal.model(zone), "preheat": thermal.preheat(zone), "calc": calc}
    template = "core/_warmup_calc.html" if request.GET.get("calc") else "core/_sheet_warmup.html"
    return render(request, template, ctx)
