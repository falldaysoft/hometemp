import json
from pathlib import Path

from django.core.management.base import BaseCommand

from core.ingest import Ingestor


class Command(BaseCommand):
    help = "Import collector JSONL logs (events-*.jsonl) straight into the database. Safe to re-run."

    def add_arguments(self, parser):
        parser.add_argument("paths", nargs="+", help="JSONL files or directories containing events-*.jsonl")

    def handle(self, paths, **opts):
        files = []
        for p in map(Path, paths):
            files += sorted(p.glob("events-*.jsonl")) if p.is_dir() else [p]
        ing = Ingestor()
        for f in files:
            with f.open() as fh:
                records = [json.loads(line) for line in fh if line.strip()]
            for i in range(0, len(records), 2000):
                ing.ingest(records[i:i + 2000])
            self.stdout.write(f"{f.name}: {len(records)} records")
        self.stdout.write(str(ing.stats))
