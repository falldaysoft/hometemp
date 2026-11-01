from django.core.management.base import BaseCommand

from core.hvac import rebuild, thermostats


class Command(BaseCommand):
    help = "Rebuild furnace/AC run intervals from raw thermostat readings (all history)."

    def handle(self, **opts):
        for acc in thermostats():
            self.stdout.write(f"{acc.name}: {rebuild(acc)} runs")
