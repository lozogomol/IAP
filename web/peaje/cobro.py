"""Lógica del peaje: votación entre frames, registro del paso con cobro y consulta del estado de una placa."""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Alerta, Caseta, LecturaPlaca, Paso, Tarifa, Vehiculo
from .placas import normalizar_placa, sugerencias

VENTANA_VOTO = timedelta(seconds=3)
ANTIDUPLICADO = timedelta(seconds=60)
MIN_VOTOS = 2
CONFIANZA_SOLA = 0.85
CERO = Decimal("0")


# --- Votación entre frames ----------------------------------------------------------------------

def votar(caseta, ahora=None):
    """Placa ganadora entre las lecturas de la caseta de los últimos 3 s, o None si aún no hay consenso.

    Gana la más repetida; vale si tiene >= 2 lecturas o alguna lectura con confianza > 0.85.
    Devuelve {"placa", "votos", "confianza", "categoria", "ejes"}.
    """
    ahora = ahora or timezone.now()
    grupos = defaultdict(list)
    for l in LecturaPlaca.objects.filter(caseta=caseta, created_at__gte=ahora - VENTANA_VOTO):
        grupos[l.placa].append(l)
    if not grupos:
        return None
    placa, lecturas = max(grupos.items(),
                          key=lambda kv: (len(kv[1]), max(l.confianza or 0 for l in kv[1])))
    mejor = max(l.confianza or 0 for l in lecturas)
    if len(lecturas) < MIN_VOTOS and mejor <= CONFIANZA_SOLA:
        return None
    con_categoria = next((l for l in lecturas if l.categoria and l.ejes), None)
    return {
        "placa": placa,
        "votos": len(lecturas),
        "confianza": mejor or None,
        "categoria": con_categoria.categoria if con_categoria else "",
        "ejes": con_categoria.ejes if con_categoria else None,
    }


def limpiar_lecturas_viejas():
    LecturaPlaca.objects.filter(created_at__lt=timezone.now() - timedelta(minutes=5)).delete()


# --- Registro del paso ---------------------------------------------------------------------------

def _tarifa(categoria, ejes):
    if not categoria or not ejes:
        return None
    t = Tarifa.objects.filter(categoria=categoria, ejes=ejes).first()
    return t.monto if t else None


def paso_reciente(caseta, placa, ahora=None):
    ahora = ahora or timezone.now()
    return Paso.objects.filter(caseta=caseta, placa_normalizada=placa,
                               created_at__gte=ahora - ANTIDUPLICADO).first()


def registrar_paso(caseta_id, placa, usuario=None, confianza=None, texto_leido=None, categoria="", ejes=None):
    """Registra el paso y cobra. Devuelve (paso, duplicado, info).

    Categoría y ejes: los de la inferencia (M2/M3) si llegaron y tienen tarifa; si no, los del vehículo
    registrado; si no está registrado, "desconocida" y monto 0. Nunca se inventan valores.
    """
    with transaction.atomic():
        caseta = Caseta.objects.select_for_update().get(pk=caseta_id)  # serializa los pasos de la caseta
        previo = paso_reciente(caseta, placa)
        if previo:
            return previo, True, {"fuente_categoria": None}

        vehiculo = Vehiculo.objects.select_for_update().filter(placa=placa).first()
        fuente = "inferencia"
        monto_tarifa = _tarifa(categoria, ejes)
        if monto_tarifa is None:
            if vehiculo:
                categoria, ejes, fuente = vehiculo.categoria, vehiculo.ejes, "registro"
                monto_tarifa = _tarifa(categoria, ejes)
            else:
                categoria, ejes, fuente = "", None, "desconocida"

        monto, alerta, nota = CERO, None, ""
        if vehiculo is None or not vehiculo.activo:
            estado, alerta = "no_registrada", "placa_no_registrada"
            if vehiculo:
                nota = "Vehículo registrado pero inactivo."
        elif monto_tarifa is None:
            estado, nota = "error_lectura", f"No hay tarifa para {categoria} con {ejes} ejes."
        elif vehiculo.saldo >= monto_tarifa:
            estado, monto = "cobrado", monto_tarifa
            vehiculo.saldo -= monto_tarifa
            vehiculo.save(update_fields=["saldo"])
        else:
            estado, alerta = "saldo_insuficiente", "saldo_insuficiente"

        paso = Paso.objects.create(
            placa_leida=(texto_leido or placa)[:20], placa_normalizada=placa, confianza_ocr=confianza,
            categoria_detectada=categoria or "", ejes_detectados=ejes, monto=monto, caseta=caseta,
            usuario=usuario, estado=estado,
        )
        if alerta:
            Alerta.objects.create(paso=paso, tipo=alerta)
    return paso, False, {"fuente_categoria": fuente, "tarifa": monto_tarifa, "nota": nota,
                         "saldo": vehiculo.saldo if vehiculo else None}


def paso_a_dict(paso, info=None):
    info = info or {}
    tarifa = info.get("tarifa")
    if tarifa is None:
        tarifa = _tarifa(paso.categoria_detectada, paso.ejes_detectados)
    return {
        "id": paso.id,
        "placa": paso.placa_normalizada,
        "estado": paso.estado,
        "estado_texto": paso.get_estado_display(),
        "categoria": paso.categoria_detectada or "desconocida",
        "ejes": paso.ejes_detectados,
        "tarifa": str(tarifa) if tarifa is not None else None,
        "monto_cobrado": str(paso.monto),
        "saldo": str(info["saldo"]) if info.get("saldo") is not None else None,
        "fuente_categoria": info.get("fuente_categoria"),
        "nota": info.get("nota", ""),
        "confianza": paso.confianza_ocr,
        "caseta": paso.caseta_id,
        "created_at": paso.created_at.isoformat(),
    }


# --- Consulta ------------------------------------------------------------------------------------

ESTADOS_CONSULTA = {
    "habilitada": "Registrada con saldo suficiente",
    "saldo_insuficiente": "Saldo insuficiente",
    "inactiva": "Registrada pero inactiva",
    "no_registrada": "Placa no registrada",
}


def consultar_placa(texto):
    """Estado de una placa: habilitada / saldo_insuficiente / inactiva / no_registrada, + sugerencias."""
    placa = normalizar_placa(texto)
    vehiculo = Vehiculo.objects.filter(placa=placa).first() if placa else None
    datos = {"placa": placa, "vehiculo": None, "tarifa": None, "sugerencias": []}
    if vehiculo is None:
        datos["estado"] = "no_registrada"
        datos["sugerencias"] = sugerencias(placa, Vehiculo.objects.values_list("placa", flat=True))[:5]
    else:
        tarifa = _tarifa(vehiculo.categoria, vehiculo.ejes)
        if not vehiculo.activo:
            datos["estado"] = "inactiva"
        elif tarifa is not None and vehiculo.saldo < tarifa:
            datos["estado"] = "saldo_insuficiente"
        else:
            datos["estado"] = "habilitada"
        datos["tarifa"] = str(tarifa) if tarifa is not None else None
        datos["vehiculo"] = {
            "placa": vehiculo.placa, "propietario": vehiculo.propietario, "categoria": vehiculo.categoria,
            "categoria_texto": vehiculo.get_categoria_display(), "ejes": vehiculo.ejes,
            "saldo": str(vehiculo.saldo), "activo": vehiculo.activo,
        }
    datos["estado_texto"] = ESTADOS_CONSULTA[datos["estado"]]
    return datos
