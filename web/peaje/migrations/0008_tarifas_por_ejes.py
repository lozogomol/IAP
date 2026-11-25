"""Tarifas por categoría y ejes: base de la categoría con 2 ejes + 2 Bs por cada eje adicional (hasta 6 ejes)."""
from decimal import Decimal

from django.db import migrations

BASE = {"moto": 2, "auto": 4, "minibus": 6, "bus": 8, "camion": 8}
CON_EJES_EXTRA = ("auto", "minibus", "bus", "camion")
EXTRA_POR_EJE = 2
MAX_EJES = 6


def filas():
    for categoria, base in BASE.items():
        for ejes in range(2, (MAX_EJES if categoria in CON_EJES_EXTRA else 2) + 1):
            yield categoria, ejes, Decimal(base + EXTRA_POR_EJE * (ejes - 2))


def cargar(apps, schema_editor):
    Tarifa = apps.get_model("peaje", "Tarifa")
    validas = set()
    for categoria, ejes, monto in filas():
        t, _ = Tarifa.objects.update_or_create(categoria=categoria, ejes=ejes, defaults={"monto": monto})
        validas.add(t.pk)
    Tarifa.objects.exclude(pk__in=validas).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("peaje", "0007_paso_en_curso"),
    ]

    operations = [
        migrations.RunPython(cargar, migrations.RunPython.noop),
    ]
