"""Caseta única que usa el modo caseta: "Peaje IAP". Las demás casetas se conservan (con su historial) sin mostrarse."""
from django.db import migrations

NOMBRE = "Peaje IAP"


def crear(apps, schema_editor):
    Caseta = apps.get_model("peaje", "Caseta")
    if not Caseta.objects.filter(nombre=NOMBRE).exists():
        Caseta.objects.create(nombre=NOMBRE, ubicacion="Peaje IAP")


class Migration(migrations.Migration):
    dependencies = [("peaje", "0003_lecturas_y_solicitudes")]
    operations = [migrations.RunPython(crear, migrations.RunPython.noop)]
