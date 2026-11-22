"""Datos del panel (KPIs, gráfica, últimos pasos) y publicación de cada paso nuevo: captura + evento en tiempo real."""
import hashlib
import logging
import time
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import close_old_connections, connection
from django.db.models import Sum
from django.utils import timezone

from . import tiempo_real
from .models import Alerta, Paso

log = logging.getLogger(__name__)
DIAS = ["Dom", "Lun", "Mar", "Mié", "Jue", "Vie", "Sáb"]


def _inicio_dia(fecha):
    return timezone.make_aware(datetime.combine(fecha, datetime.min.time()))


def resumen_paso(paso):
    local = timezone.localtime(paso.created_at)
    return {
        "id": paso.id,
        "placa": paso.placa_normalizada,
        "estado": paso.estado,
        "estado_texto": paso.get_estado_display(),
        "monto": f"{paso.monto:.2f}",
        "hora": local.strftime("%H:%M:%S"),
        "fecha": local.strftime("%d/%m/%Y"),
        "imagen_url": paso.imagen_url or "",
    }


def kpis():
    hoy = Paso.objects.filter(created_at__gte=_inicio_dia(timezone.localdate()))
    recaudado = hoy.filter(estado="cobrado").aggregate(t=Sum("monto"))["t"] or Decimal("0")
    return {
        "pasos_hoy": hoy.count(),
        "recaudacion_hoy": f"{recaudado:.2f}",
        "alertas_pendientes": Alerta.objects.filter(atendida=False).count(),
    }


def grafica(dias=7):
    """Pasos por día (fecha local) de los últimos `dias` días, incluido hoy."""
    hoy = timezone.localdate()
    fechas = [hoy - timedelta(days=i) for i in range(dias - 1, -1, -1)]
    conteo = dict.fromkeys(fechas, 0)
    for creado in Paso.objects.filter(created_at__gte=_inicio_dia(fechas[0])).values_list("created_at", flat=True):
        dia = timezone.localtime(creado).date()
        if dia in conteo:
            conteo[dia] += 1
    return {
        "etiquetas": [f"{DIAS[(f.weekday() + 1) % 7]} {f.day}" for f in fechas],
        "valores": [conteo[f] for f in fechas],
    }


def ultimos_pasos(n=10):
    return [resumen_paso(p) for p in Paso.objects.order_by("-created_at")[:n]]


def datos_panel():
    return {"kpis": kpis(), "grafica": grafica(), "pasos": ultimos_pasos()}


def evento_paso(paso):
    alerta = paso.alertas.first()
    return {
        "paso": resumen_paso(paso),
        "alerta": {"tipo": alerta.tipo, "tipo_texto": alerta.get_tipo_display()} if alerta else None,
        "kpis": kpis(),
        "grafica": grafica(),
    }


# --- Capturas --------------------------------------------------------------------------------------

def _subir_cloudinary(imagen, nombre):
    u = urlparse(settings.CLOUDINARY_URL)
    params = {"folder": "iap/capturas", "public_id": nombre, "timestamp": str(int(time.time()))}
    firma = hashlib.sha1(("&".join(f"{k}={params[k]}" for k in sorted(params)) + (u.password or "")).encode())
    r = requests.post(f"https://api.cloudinary.com/v1_1/{u.hostname}/image/upload",
                      data={**params, "api_key": u.username, "signature": firma.hexdigest()},
                      files={"file": (f"{nombre}.jpg", imagen, "image/jpeg")}, timeout=15)
    r.raise_for_status()
    return r.json()["secure_url"]


def _guardar_local(imagen, paso):
    nombre = default_storage.save(
        f"capturas/{timezone.localtime(paso.created_at):%Y/%m}/paso_{paso.id}.jpg", ContentFile(imagen))
    return "/" + nombre.replace("\\", "/")


def _subir_y_notificar(paso_id, imagen):
    """Hilo aparte: sube a Cloudinary, guarda la URL y avisa por Pusher."""
    close_old_connections()
    try:
        try:
            url = _subir_cloudinary(imagen, f"paso_{paso_id}")
            Paso.objects.filter(pk=paso_id).update(imagen_url=url)
        except (requests.RequestException, KeyError, ValueError) as e:
            log.warning("No se pudo subir la captura del paso %s: %s", paso_id, e)
        paso = Paso.objects.filter(pk=paso_id).first()
        if paso and tiempo_real.configurado():
            tiempo_real._enviar("paso", evento_paso(paso))
    finally:
        connection.close()


def publicar_paso(paso, imagen=None):
    """Guarda la captura del paso recién registrado y lo emite en tiempo real."""
    if imagen and settings.CLOUDINARY_URL:
        tiempo_real.en_segundo_plano(_subir_y_notificar, paso.id, imagen)
        return
    if imagen:
        try:
            paso.imagen_url = _guardar_local(imagen, paso)
            paso.save(update_fields=["imagen_url"])
        except OSError as e:
            log.warning("No se pudo guardar la captura del paso %s: %s", paso.id, e)
    tiempo_real.notificar("paso", evento_paso(paso))
