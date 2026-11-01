"""Ingest collector records.

The wire format is the collector's own JSONL record shape (see collector/Sources/Collector.swift),
so the collector can upload its log lines verbatim and old spike logs can be backfilled unchanged.
Idempotent: readings are unique on (characteristic, ts).
"""
import uuid
from datetime import datetime

from django.db import transaction

from .models import Accessory, Alias, Characteristic, RawRecord, Reading, Room, Zone

SOURCES = {"event": Reading.Source.EVENT, "heartbeat": Reading.Source.HEARTBEAT, "initial": Reading.Source.INITIAL}


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def numeric(v):
    """HomeKit values → float. Booleans become 0/1; strings and nulls are skipped."""
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    return None


def find_accessory(acc_uuid, name, manufacturer=None, model=None):
    """Our accessory for a HomeKit ID: by ID, by alias, or (for an ID from another collector Mac) by name and
    make. A match adds an alias so later records resolve directly. None if it's genuinely new."""
    acc = Accessory.objects.filter(uuid=acc_uuid).first()
    if acc:
        return acc
    alias = Alias.objects.select_related("accessory").filter(uuid=acc_uuid, accessory__isnull=False).first()
    if alias:
        return alias.accessory
    candidates = list(Accessory.objects.filter(name=name)) if name else []
    if manufacturer:
        same = [a for a in candidates if (a.manufacturer, a.model) == (manufacturer, model or "")]
        candidates = same or [a for a in candidates if not a.manufacturer]
    if len(candidates) == 1:
        Alias.objects.get_or_create(uuid=acc_uuid, defaults={"accessory": candidates[0]})
        return candidates[0]
    return None


def find_characteristic(ch_uuid, acc, kind, service_type):
    ch = Characteristic.objects.select_related("accessory").filter(uuid=ch_uuid).first()
    if ch:
        return ch
    alias = Alias.objects.select_related("characteristic__accessory").filter(
        uuid=ch_uuid, characteristic__isnull=False).first()
    if alias:
        return alias.characteristic
    if acc is None or acc.pk is None:
        return None
    candidates = [c for c in acc.characteristics.filter(kind=kind)
                  if not service_type or not c.service_type or c.service_type == service_type]
    if len(candidates) == 1:
        Alias.objects.get_or_create(uuid=ch_uuid, defaults={"characteristic": candidates[0]})
        return candidates[0]
    return None


class Ingestor:
    def __init__(self, device=None):
        self.device = device
        self.rooms = {}
        self.accessories = {}
        self.characteristics = {}
        self.stats = {"readings": 0, "raw": 0, "skipped": 0, "accessories": 0}
        self.latest_ts = None

    def room(self, name):
        if not name:
            return None
        if name not in self.rooms:
            room, created = Room.objects.get_or_create(name=name)
            if created and room.zone_id is None:
                # Default mapping: HomeKit's "Office" room is the Office zone; everything else the Home side.
                zone_name = "Office" if name.lower() == "office" else "Home"
                room.zone = Zone.objects.filter(name=zone_name).first()
                room.save(update_fields=["zone"])
            self.rooms[name] = room
        return self.rooms[name]

    def accessory(self, acc_uuid, name, room_name, ts, kinds=(), **extra):
        key = str(acc_uuid)
        acc = self.accessories.get(key)
        if acc is None:
            acc = find_accessory(acc_uuid, name, extra.get("manufacturer"), extra.get("model"))
            if acc is None:
                acc = Accessory(uuid=acc_uuid, name=name, first_seen=ts, last_seen=ts,
                                role=Accessory.suggest_role(name, kinds))
                self.stats["accessories"] += 1
            self.accessories[key] = acc
        acc.name = name or acc.name
        acc.homekit_room = self.room(room_name) or acc.homekit_room
        for k, v in extra.items():
            setattr(acc, k, v)
        if ts > acc.last_seen:
            acc.last_seen = ts
        if acc.role == Accessory.Role.UNCLASSIFIED and kinds:
            acc.role = Accessory.suggest_role(acc.name, kinds)
        acc.save()
        return acc

    def characteristic(self, rec, ts):
        key = rec["characteristicUUID"]
        ch = self.characteristics.get(key)
        if ch is None:
            acc = self.accessory(uuid.UUID(rec["accessoryUUID"]), rec.get("accessory", ""), rec.get("room"), ts,
                                 kinds=[rec["characteristic"]])
            ch = find_characteristic(key, acc, rec["characteristic"][:60], rec.get("service", ""))
            if ch is None:
                ch = Characteristic(uuid=key, accessory=acc)
            # Newer collector builds name more characteristics and add HomeKit's own label; keep the latest.
            fields = {"kind": rec["characteristic"][:60], "type_uuid": rec.get("characteristicType", ch.type_uuid),
                      "description": rec.get("description", ch.description)[:100],
                      "unit": rec.get("unit", ch.unit), "format": rec.get("format", ch.format),
                      "service_type": rec.get("service", ch.service_type)[:40]}
            if ch.pk is None or any(getattr(ch, k) != v for k, v in fields.items()):
                for k, v in fields.items():
                    setattr(ch, k, v)
                ch.save()
            self.characteristics[key] = ch
        return ch

    def add(self, rec):
        kind = rec.get("type")
        ts = parse_ts(rec["ts"]) if "ts" in rec else None
        if ts and (self.latest_ts is None or ts > self.latest_ts):
            self.latest_ts = ts
        if kind == "inventory":
            for a in rec.get("accessories", []):
                kinds = [c["type"] for s in a.get("services", []) for c in s.get("characteristics", [])]
                self.accessory(uuid.UUID(a["uuid"]), a["name"], a.get("room"), ts, kinds=kinds,
                               manufacturer=a.get("manufacturer", ""), model=a.get("model", ""),
                               bridged=bool(a.get("bridged")), reachable=bool(a.get("reachable", True)))
        elif kind == "reachability":
            acc = find_accessory(rec["uuid"], rec.get("accessory", ""))
            if acc:
                acc.reachable = bool(rec.get("reachable"))
                acc.save(update_fields=["reachable"])
        if kind == "value":
            value = numeric(rec.get("value"))
            if str(rec.get("characteristic", "")).startswith("000000") and "description" not in rec:
                self.stats["skipped"] += 1  # pre-mapping raw-UUID row from early spike builds; superseded
                return
            ch = self.characteristic(rec, ts)
            if value is not None:
                self.pending.append(Reading(characteristic=ch, ts=ts, value=value,
                                            source=SOURCES.get(rec.get("source"), Reading.Source.EVENT)))
                return
        # Everything that isn't a numeric reading is archived verbatim.
        if ts is not None:
            self.raw.append(RawRecord(ts=ts, type=str(kind)[:40], data=rec, digest=RawRecord.digest_of(rec), device=self.device))

    def ingest(self, records):
        self.pending = []
        self.raw = []
        with transaction.atomic():
            for rec in records:
                self.add(rec)
            created = Reading.objects.bulk_create(self.pending, ignore_conflicts=True, batch_size=1000)
            self.stats["readings"] += len(created)  # includes duplicates ignored by the DB
            self.stats["raw"] += len(RawRecord.objects.bulk_create(self.raw, ignore_conflicts=True, batch_size=500))
        return self.stats
