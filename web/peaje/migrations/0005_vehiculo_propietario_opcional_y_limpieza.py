"""Propietario opcional (no se muestra) y limpieza única de datos de demostración.

Borra todos los pasos, alertas y lecturas, y las imágenes de evidencia (disco local y la carpeta
iap/capturas de Cloudinary si está configurado). Conserva usuarios, casetas, tarifas y los 20 vehículos
(con el propietario vaciado). Como toda migración, Render la ejecuta una sola vez.
"""
import logging
import shutil
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.db import migrations, models

log = logging.getLogger(__name__)


def _borrar_imagenes():
    if getattr(settings, "TESTING", False):
        return  # nunca tocar archivos reales al crear la BD de tests
    shutil.rmtree(Path(settings.MEDIA_ROOT) / "capturas", ignore_errors=True)
    if settings.CLOUDINARY_URL:
        try:
            import requests
            u = urlparse(settings.CLOUDINARY_URL)
            r = requests.delete(f"https://api.cloudinary.com/v1_1/{u.hostname}/resources/image/upload",
                                params={"prefix": "iap/capturas"}, auth=(u.username, u.password), timeout=20)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001 - la limpieza de imágenes no debe frenar el deploy
            log.warning("No se pudieron borrar las capturas de Cloudinary: %s", e)


def limpiar(apps, schema_editor):
    apps.get_model("peaje", "Alerta").objects.all().delete()
    apps.get_model("peaje", "Paso").objects.all().delete()
    apps.get_model("peaje", "LecturaPlaca").objects.all().delete()
    apps.get_model("peaje", "Vehiculo").objects.update(propietario="")
    _borrar_imagenes()


class Migration(migrations.Migration):

    dependencies = [
        ('peaje', '0004_caseta_peaje_iap'),
    ]

    operations = [
        migrations.AlterField(
            model_name='vehiculo',
            name='propietario',
            field=models.CharField(blank=True, default='', max_length=120),
        ),
        migrations.RunPython(limpiar, migrations.RunPython.noop),
    ]
