from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from core.hvac import rebuild, thermostats
from core.models import Accessory, Alias, Reading, Zone


class Command(BaseCommand):
    help = ("Merge accessories duplicated by a second collector Mac (HomeKit IDs differ per device): keep the "
            "oldest record and its settings, move readings over, and alias the other IDs. Safe to re-run.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    @transaction.atomic
    def handle(self, dry_run, **opts):
        groups = defaultdict(list)
        for a in Accessory.objects.order_by("first_seen", "id"):
            groups[(a.name, a.manufacturer, a.model)].append(a)
        merged = 0
        for (name, *_), accs in groups.items():
            if len(accs) < 2:
                continue
            keeper, dups = accs[0], accs[1:]
            for dup in dups:
                self.stdout.write(f"{name}: merging {dup.uuid} into {keeper.uuid}")
                if dry_run:
                    continue
                for ch in list(dup.characteristics.all()):
                    target = next((c for c in keeper.characteristics.filter(kind=ch.kind)
                                   if not ch.service_type or not c.service_type or c.service_type == ch.service_type),
                                  None)
                    if target is None:
                        ch.accessory = keeper
                        ch.save(update_fields=["accessory"])
                        continue
                    clash = Reading.objects.filter(characteristic=target).values_list("ts", flat=True)
                    Reading.objects.filter(characteristic=ch, ts__in=clash).delete()
                    Reading.objects.filter(characteristic=ch).update(characteristic=target)
                    Alias.objects.filter(characteristic=ch).update(characteristic=target)
                    Alias.objects.get_or_create(uuid=ch.uuid, defaults={"characteristic": target})
                    ch.delete()
                Alias.objects.filter(accessory=dup).update(accessory=keeper)
                Zone.objects.filter(thermostat=dup).update(thermostat=keeper)
                Alias.objects.get_or_create(uuid=dup.uuid, defaults={"accessory": keeper})
                if dup.last_seen > keeper.last_seen:
                    keeper.last_seen, keeper.reachable = dup.last_seen, dup.reachable
                    keeper.save(update_fields=["last_seen", "reachable"])
                dup.delete()
                merged += 1
        if not dry_run:
            for acc in thermostats():
                rebuild(acc)
        self.stdout.write(f"merged {merged} duplicate accessories")
