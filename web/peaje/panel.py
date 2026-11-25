"""Datos del panel (KPIs, gráfica, últimos pasos) y publicación de cada paso nuevo: captura + evento en tiempo real."""
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from . import evidencias, tiempo_real
from .models import CATEGORIAS, Alerta, Paso

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
        "categoria": paso.categoria_detectada,
        "categoria_texto": paso.get_categoria_detectada_display(),
        "ejes": paso.ejes_detectados,
        "pasajeros": paso.pasajeros,
        "ejes_texto": paso.ejes_texto,
        "monto": f"{paso.monto:.2f}",
        "tarifa": f"{paso.tarifa:.2f}" if paso.tarifa is not None else None,
        "cobrado": paso.cobrado,
        "forma_pago": paso.forma_pago,
        "registrada": paso.registrada,
        "motivo_pendiente": paso.motivo_pendiente if paso.pendiente else "",
        "resultado_texto": paso.resultado_texto,
        "hora": local.strftime("%H:%M:%S"),
        "fecha": local.strftime("%d/%m/%Y"),
        "imagen_url": paso.imagen_url or "",
    }


def kpis():
    hoy = Paso.objects.filter(created_at__gte=_inicio_dia(timezone.localdate()))
    # Recaudación = todos los pasos cobrados (con saldo + en efectivo); los pendientes aún no suman.
    por_forma = dict(hoy.filter(estado="cobrado").order_by().values_list("forma_pago").annotate(t=Sum("monto")))
    saldo, efectivo = por_forma.get("saldo") or Decimal("0"), por_forma.get("efectivo") or Decimal("0")
    recaudado = sum(por_forma.values(), Decimal("0"))
    return {
        "pasos_hoy": hoy.count(),
        "recaudacion_hoy": f"{recaudado:.2f}",
        "recaudacion_saldo": f"{saldo:.2f}",
        "recaudacion_efectivo": f"{efectivo:.2f}",
        "alertas_pendientes": Alerta.objects.filter(atendida=False).count(),
        "por_tipo": por_tipo(hoy),
    }


def por_tipo(pasos):
    """Vehículos de hoy por tipo (M2), en el orden de las categorías; los sin tipo van como "Sin tipo"."""
    conteo = dict(pasos.order_by().values_list("categoria_detectada").annotate(n=Count("id")))
    filas = [{"categoria": c, "texto": t, "n": conteo.get(c, 0)} for c, t in CATEGORIAS]
    if conteo.get(""):
        filas.append({"categoria": "", "texto": "Sin tipo", "n": conteo[""]})
    return filas


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
    return [resumen_paso(p) for p in Paso.objects.prefetch_related("alertas").order_by("-created_at")[:n]]


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


def publicar_pago(paso):
    """Un paso pendiente se cobró en efectivo: el panel y las alertas se actualizan sin recargar."""
    alerta = paso.alertas.order_by("-atendida_at").first()
    tiempo_real.notificar("pago", {
        "paso": resumen_paso(paso),
        "alerta": {"id": alerta.id, "tipo": alerta.tipo, "nota": alerta.nota} if alerta else None,
        "kpis": kpis(),
    })


# --- Capturas ---------------------------------------------------------------------------------------

def publicar_paso(paso, imagen=None):
    """Guarda la evidencia del paso recién registrado EN LA BASE DE DATOS y lo emite en tiempo real."""
    if imagen:
        evidencias.guardar_evidencia(paso, imagen)
    tiempo_real.notificar("paso", evento_paso(paso))
