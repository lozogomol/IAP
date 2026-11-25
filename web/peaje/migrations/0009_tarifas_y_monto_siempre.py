"""Vuelve a fijar las tarifas vigentes (por si la 0008 no llegó a la base de producción) y rellena el monto de
los pasos no cobrados: desde ahora `monto` es siempre la tarifa calculada; el estado dice si se cobró."""
from decimal import Decimal

from django.db import migrations

BASE = {"moto": 2, "auto": 4, "minibus": 6, "bus": 8, "camion": 8}
CON_EJES_EXTRA = ("auto", "minibus", "bus", "camion")
EXTRA_POR_EJE = 2
MAX_EJES = 6


def _monto(categoria, ejes):
    base = BASE.get(categoria)
    if base is None:
        return None
    return Decimal(base + EXTRA_POR_EJE * (max(2, ejes or 2) - 2))


def aplicar(apps, schema_editor):
    Tarifa = apps.get_model("peaje", "Tarifa")
    Paso = apps.get_model("peaje", "Paso")
    validas = set()
    for categoria in BASE:
        for ejes in range(2, (MAX_EJES if categoria in CON_EJES_EXTRA else 2) + 1):
            t, _ = Tarifa.objects.update_or_create(categoria=categoria, ejes=ejes,
                                                   defaults={"monto": _monto(categoria, ejes)})
            validas.add(t.pk)
    Tarifa.objects.exclude(pk__in=validas).delete()

    for paso in Paso.objects.exclude(estado="cobrado").filter(monto=0).exclude(categoria_detectada=""):
        monto = _monto(paso.categoria_detectada, paso.ejes_detectados)
        if monto is not None:
            Paso.objects.filter(pk=paso.pk).update(monto=monto)


class Migration(migrations.Migration):

    dependencies = [
        ("peaje", "0008_tarifas_por_ejes"),
    ]

    operations = [
        migrations.RunPython(aplicar, migrations.RunPython.noop),
    ]
