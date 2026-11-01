from django.db import migrations


def seed(apps, schema_editor):
    Zone = apps.get_model("core", "Zone")
    Zone.objects.get_or_create(name="Home", defaults={"order": 0})
    Zone.objects.get_or_create(name="Office", defaults={"order": 1})


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
