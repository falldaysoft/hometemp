from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from .dashboard import accessory_detail, dashboard
from .models import Accessory, Room, Zone


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
