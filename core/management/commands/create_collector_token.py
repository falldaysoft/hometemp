from django.core.management.base import BaseCommand

from core.models import CollectorDevice


class Command(BaseCommand):
    help = "Create (or rotate) an upload token for a collector device. The token is printed once."

    def add_arguments(self, parser):
        parser.add_argument("name", help="Device name, e.g. idlemac")

    def handle(self, name, **opts):
        self.stdout.write(CollectorDevice.issue(name))
