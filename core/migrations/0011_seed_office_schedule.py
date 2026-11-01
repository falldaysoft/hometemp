from datetime import time

from django.db import migrations


def seed(apps, schema_editor):
    Zone = apps.get_model("core", "Zone")
    ZoneSchedule = apps.get_model("core", "ZoneSchedule")
    office = Zone.objects.filter(name="Office").first()
    if office and not ZoneSchedule.objects.filter(zone=office).exists():
        ZoneSchedule.objects.create(zone=office, weekdays="0,1,2,3,4", start=time(8, 30), end=time(17, 0))


class Migration(migrations.Migration):
    dependencies = [("core", "0010_occupancy")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
