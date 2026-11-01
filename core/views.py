from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from . import weather
from .cost import daily
from .dashboard import accessory_detail, dashboard, outdoor_detail
from .models import Accessory, HomeSettings, Room, Zone


def health(request):
    # The container healthcheck in deploy/docker-compose.yml hits this.
    return HttpResponse("ok")


@login_required
def index(request):
    template = "core/_dashboard.html" if request.headers.get("HX-Request") else "core/index.html"
    return render(request, template, dashboard())


def _sheet(request, acc, saved=False, status=200):
    hours = 168 if request.GET.get("range") == "week" else 24
    ctx = {**accessory_detail(acc, hours=hours), "rooms": Room.objects.select_related("zone").order_by("zone__order", "name"),
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
            s.save()
            saved = True
            try:
                weather.fetch(s, past_days=14)  # backfill right away so the tile isn't empty
            except Exception:
                pass
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
