import time
import traceback
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import HomeSettings
from core.weather import fetch

INTERVAL = timedelta(minutes=15)


class Command(BaseCommand):
    help = ("Fetch outdoor weather from Open-Meteo. With --loop, checks every minute and fetches every 15 min, "
            "or right away (with a 14-day backfill) when the location changes.")

    def add_arguments(self, parser):
        # Accepts the older "--loop 900" form too; either way it checks every minute.
        parser.add_argument("--loop", nargs="?", const=60, type=int, default=0)
        parser.add_argument("--past-days", type=int, default=2)

    def handle(self, loop, past_days, **opts):
        while True:
            s = HomeSettings.load()
            due = s.has_location and (s.location_changed or s.weather_fetched_at is None
                                      or timezone.now() - s.weather_fetched_at >= INTERVAL)
            if due or not loop:
                backfill = s.location_changed or s.weather_fetched_at is None
                try:
                    stats = fetch(s, past_days=14 if backfill else past_days)
                    HomeSettings.objects.filter(pk=s.pk).update(
                        weather_fetched_at=timezone.now(), weather_error="", location_changed=False)
                    self.stdout.write(f"{timezone.now():%H:%M} {stats}")
                except Exception as e:
                    traceback.print_exc()
                    HomeSettings.objects.filter(pk=s.pk).update(
                        weather_fetched_at=timezone.now(), weather_error=f"{type(e).__name__}: {e}"[:300])
            if not loop:
                return
            time.sleep(60)
