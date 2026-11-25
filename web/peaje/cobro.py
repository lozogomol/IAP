"""Lógica del peaje: votación entre frames, registro del paso con cobro y consulta del estado de una placa."""
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Alerta, Caseta, LecturaPlaca, Paso, Tarifa, Vehiculo
from .placas import normalizar_placa, sugerencias

VENTANA_VOTO = timedelta(seconds=4)
ANTIDUPLICADO = timedelta(seconds=30)
MIN_VOTOS = 2
CONFIANZA_SOLA = 0.80  # una sola lectura con confianza >= 0.80 basta
CERO = Decimal("0")


# --- Votación entre frames ----------------------------------------------------------------------

def votar(caseta, ahora=None, desde=None):
    """Placa ganadora entre las lecturas de la caseta de los últimos 4 s, o None si aún no hay consenso.

    Gana la más repetida; vale si tiene >= 2 lecturas o alguna lectura con confianza >= 0.80. `desde`: ignora las
    lecturas anteriores (las del vehículo que ya se registró).
    Devuelve {"placa", "votos", "confianza", "categoria", "ejes", "pasajeros"}.
    """
    ahora = ahora or timezone.now()
    inicio = ahora - VENTANA_VOTO if desde is None else max(ahora - VENTANA_VOTO, desde)
    grupos = defaultdict(list)
    for l in LecturaPlaca.objects.filter(caseta=caseta, created_at__gte=inicio):
        grupos[l.placa].append(l)
    if not grupos:
        return None
    placa, lecturas = max(grupos.items(),
                          key=lambda kv: (len(kv[1]), max(l.confianza or 0 for l in kv[1])))
    mejor = max(l.confianza or 0 for l in lecturas)
    if len(lecturas) < MIN_VOTOS and mejor < CONFIANZA_SOLA:
        return None
    # Categoría (M2): la más repetida entre las lecturas de la placa ganadora; ejes (M3) y pasajeros (M4): el
    # valor más repetido entre las lecturas que los traen (a igualdad, el mayor). Nunca se inventan.
    votos_cat = Counter(l.categoria for l in lecturas if l.categoria)
    categoria = votos_cat.most_common(1)[0][0] if votos_cat else ""
    de_categoria = [l for l in lecturas if l.categoria == categoria] if categoria else lecturas
    return {
        "placa": placa,
        "votos": len(lecturas),
        "confianza": mejor or None,
        "categoria": categoria,
        "ejes": _moda(l.ejes for l in de_categoria),
        "pasajeros": _moda(l.pasajeros for l in lecturas),
    }


def _moda(valores):
    conteo = Counter(v for v in valores if v is not None)
    if not conteo:
        return None
    return max(conteo.items(), key=lambda kv: (kv[1], kv[0]))[0]


def votos_de(caseta, placa, ahora=None):
    """Lecturas de esa placa en la caseta dentro de la ventana de votación."""
    ahora = ahora or timezone.now()
    return LecturaPlaca.objects.filter(caseta=caseta, placa=placa, created_at__gte=ahora - VENTANA_VOTO).count()


def limpiar_lecturas_viejas():
    LecturaPlaca.objects.filter(created_at__lt=timezone.now() - timedelta(minutes=5)).delete()


# --- Registro del paso ---------------------------------------------------------------------------

# Tarifa base (2 ejes) por categoría y recargo por eje adicional: respaldo si falta la fila en la tabla `tarifas`.
TARIFA_BASE = {"moto": Decimal("2"), "auto": Decimal("4"), "minibus": Decimal("6"), "bus": Decimal("8"),
               "camion": Decimal("8")}
EXTRA_POR_EJE = Decimal("2")
EJES_MIN = 2


def tarifa_de(categoria, ejes):
    """(monto, ejes_cobrados) de la categoría con los ejes detectados, o None si no hay categoría con tarifa.

    Los ejes se cuentan con mínimo 2 (sin dato de M3 = 2 ejes). Usa la fila de la tabla `tarifas`; si esa
    combinación no existe, base de la categoría (fila de 2 ejes) + 2 Bs por cada eje adicional."""
    if not categoria:
        return None
    ejes = max(EJES_MIN, ejes or EJES_MIN)
    exacta = Tarifa.objects.filter(categoria=categoria, ejes=ejes).first()
    if exacta:
        return exacta.monto, ejes
    fila_base = Tarifa.objects.filter(categoria=categoria, ejes=EJES_MIN).first()
    base = fila_base.monto if fila_base else TARIFA_BASE.get(categoria)
    if base is None:
        return None
    return base + EXTRA_POR_EJE * (ejes - EJES_MIN), ejes


def _tarifa(categoria, ejes):
    t = tarifa_de(categoria, ejes)
    return t[0] if t else None


def paso_reciente(caseta, placa, ahora=None):
    ahora = ahora or timezone.now()
    return Paso.objects.filter(caseta=caseta, placa_normalizada=placa,
                               created_at__gte=ahora - ANTIDUPLICADO).first()


def registrar_paso(caseta_id, placa, usuario=None, confianza=None, texto_leido=None, categoria="", ejes=None,
                   pasajeros=None, ahora=None):
    """Registra el paso y cobra. Devuelve (paso, duplicado, info).

    Tarifa = categoría detectada (M2) + ejes detectados (M3, mínimo 2); ver tarifa_de.
    Si no se detectó categoría, la del vehículo registrado; si no está registrado ni se detectó, "desconocida" y
    monto 0. Los ejes y pasajeros que no se detectaron quedan en null en el paso: nunca se inventan.
    """
    with transaction.atomic():
        caseta = Caseta.objects.select_for_update().get(pk=caseta_id)  # serializa los pasos de la caseta
        previo = paso_reciente(caseta, placa, ahora)
        if previo:
            return previo, True, {"fuente_categoria": None}

        vehiculo = Vehiculo.objects.select_for_update().filter(placa=placa).first()
        fuente = "inferencia"
        monto_tarifa = _tarifa(categoria, ejes)
        if monto_tarifa is None:
            if vehiculo:
                categoria, ejes, fuente = vehiculo.categoria, vehiculo.ejes, "registro"
                monto_tarifa = _tarifa(categoria, ejes)
            elif not categoria:
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
            usuario=usuario, estado=estado, pasajeros=pasajeros,
        )
        if alerta:
            Alerta.objects.create(paso=paso, tipo=alerta)
    t = tarifa_de(categoria, ejes)
    return paso, False, {"fuente_categoria": fuente, "tarifa": monto_tarifa, "ejes_tarifa": t[1] if t else None,
                         "nota": nota,
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
        "categoria_texto": paso.get_categoria_detectada_display() or "Desconocida",
        "ejes": paso.ejes_detectados,
        "ejes_tarifa": info.get("ejes_tarifa") or _ejes_tarifa(paso),
        "pasajeros": paso.pasajeros,
        "tarifa": str(tarifa) if tarifa is not None else None,
        "monto_cobrado": str(paso.monto),
        "saldo": str(info["saldo"]) if info.get("saldo") is not None else None,
        "fuente_categoria": info.get("fuente_categoria"),
        "nota": info.get("nota", ""),
        "confianza": paso.confianza_ocr,
        "caseta": paso.caseta_id,
        "imagen_url": paso.imagen_url or None,
        "created_at": paso.created_at.isoformat(),
    }


def _ejes_tarifa(paso):
    t = tarifa_de(paso.categoria_detectada, paso.ejes_detectados)
    return t[1] if t else None


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
    ultimo = (Paso.objects.select_related("caseta").filter(placa_normalizada=placa).order_by("-created_at").first()
              if placa else None)
    datos["ultimo_paso"] = None
    if ultimo:
        local = timezone.localtime(ultimo.created_at)
        datos["ultimo_paso"] = {
            "fecha": local.strftime("%d/%m/%Y"), "hora": local.strftime("%H:%M:%S"),
            "created_at": ultimo.created_at.isoformat(), "estado": ultimo.estado,
            "estado_texto": ultimo.get_estado_display(), "monto": str(ultimo.monto),
            "categoria": ultimo.categoria_detectada or None,
            "categoria_texto": ultimo.get_categoria_detectada_display() or None,
            "ejes": ultimo.ejes_detectados, "pasajeros": ultimo.pasajeros,
            "imagen_url": ultimo.imagen_url or None,
            "caseta": ultimo.caseta.nombre,
        }
    return datos
