import time
import traceback

from django.core.management.base import BaseCommand

from core.weather import fetch


class Command(BaseCommand):
    help = "Fetch outdoor weather from Open-Meteo. --loop N repeats every N seconds (the scheduler container)."

    def add_arguments(self, parser):
        parser.add_argument("--loop", type=int, default=0)
        parser.add_argument("--past-days", type=int, default=2)

    def handle(self, loop, past_days, **opts):
        first = True
        while True:
            try:
                # On the first pass, backfill two weeks so charts have outdoor context right away.
                self.stdout.write(str(fetch(past_days=14 if first else past_days)))
            except Exception:
                traceback.print_exc()
            first = False
            if not loop:
                return
            time.sleep(loop)
