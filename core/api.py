import json

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .ingest import Ingestor
from .models import CollectorDevice

MAX_RECORDS = 5000


@csrf_exempt
@require_POST
def ingest(request):
    """POST /api/ingest  Authorization: Bearer <token>  body: {"records": [<collector JSONL records>]}"""
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    device = CollectorDevice.objects.filter(token_hash=CollectorDevice.hash_token(token)).first() if token else None
    if device is None:
        return JsonResponse({"error": "unauthorized"}, status=401)
    try:
        records = json.loads(request.body)["records"]
        assert isinstance(records, list)
    except (ValueError, KeyError, AssertionError):
        return JsonResponse({"error": "body must be {\"records\": [...]}"}, status=400)
    if len(records) > MAX_RECORDS:
        return JsonResponse({"error": f"at most {MAX_RECORDS} records per request"}, status=413)

    ing = Ingestor(device)
    try:
        stats = ing.ingest(records)
    except (KeyError, ValueError, TypeError) as e:
        return JsonResponse({"error": f"bad record: {e}"}, status=400)
    device.last_seen = timezone.now()
    if ing.latest_ts and (device.last_record_ts is None or ing.latest_ts > device.last_record_ts):
        device.last_record_ts = ing.latest_ts
    device.save(update_fields=["last_seen", "last_record_ts"])
    # The collector trims its local buffer up to `through`.
    return JsonResponse({"accepted": len(records), "through": ing.latest_ts.isoformat() if ing.latest_ts else None,
                         **stats})
