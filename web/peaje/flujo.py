"""Flujo automático del punto de cobro: UN VEHÍCULO = UN COBRO.

Máquina de estados por caseta (fila PasoEnCurso), alimentada por los frames de la cámara:

  ESPERA      Sin vehículo. Cada lectura de placa vota (2 lecturas iguales en 4 s o 1 con confianza >= 0.80).
  CAPTURA     Placa confirmada y BLOQUEADA: no cambia aunque otros frames lean otra cosa. Se acumulan por mayoría
              el TIPO (M2), los EJES (M3) y los PASAJEROS (M4) de ese vehículo. En cuanto hay tipo y ejes por
              mayoría, o a los 6 s del bloqueo, se registra y cobra UNA sola vez. Si a los 6 s falta el tipo: el
              del vehículo registrado; si no está registrado, la clase M2 más vista; si nunca se vio, "auto". Si
              faltan los ejes: 2 (mínimo de la categoría), marcados como estimados (ejes_detectados = null).
  RESULTADO   El paso registrado queda FIJO. Mientras se vea la misma placa o ninguna, no se recalcula ni se
  FIJO        registra nada. Solo una placa DISTINTA confirmada por votación inicia un vehículo nuevo; en ese
              momento, si el paso anterior quedó "pendiente_pago", se cobra en efectivo (cobro.cobrar_en_efectivo).
              La misma placa vuelve a cobrarse solo después de otra placa distinta o tras 10 min sin verla.
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import cobro
from .models import CATEGORIAS, LecturaPlaca, PasoEnCurso, Vehiculo

MAX_CAPTURA = timedelta(seconds=6)    # tope para juntar tipo y ejes después de bloquear la placa
REPETIR = timedelta(minutes=10)       # la misma placa sin verse este tiempo vuelve a ser un vehículo nuevo
VOTOS_TIPO = 2
VOTOS_EJES = 2
VOTOS_PASAJEROS = 2
TIPO_RESPALDO = "auto"
TEXTO_CATEGORIA = dict(CATEGORIAS)


# --- Conteos -------------------------------------------------------------------------------------

def _sumar(votos, valor):
    if valor is None or valor == "":
        return
    clave = str(valor)
    votos[clave] = votos.get(clave, 0) + 1


def _lider(votos):
    """(valor, votos, ventaja sobre el segundo) del más votado; a igualdad, el mayor valor."""
    if not votos:
        return None, 0, 0
    orden = sorted(votos.items(), key=lambda kv: (kv[1], _num(kv[0])), reverse=True)
    segundo = orden[1][1] if len(orden) > 1 else 0
    return orden[0][0], orden[0][1], orden[0][1] - segundo


def _num(clave):
    try:
        return int(clave)
    except (TypeError, ValueError):
        return clave


def tipo_actual(estado):
    return estado.tipo or (_lider(estado.votos_tipo)[0] or "")


def ejes_actuales(estado):
    valor, votos, _ = _lider(estado.votos_ejes)
    return (int(valor) if valor is not None else None), votos >= VOTOS_EJES


def pasajeros_estables(estado):
    """Máximo conteo visto en >= 2 frames; (valor, estable). Sin conteo estable, el más repetido."""
    estables = [int(k) for k, n in estado.votos_pasajeros.items() if n >= VOTOS_PASAJEROS]
    if estables:
        return max(estables), True
    valor = _lider(estado.votos_pasajeros)[0]
    return (int(valor) if valor is not None else None), False


def _bloquear_tipo(estado):
    if estado.tipo:
        return
    valor, votos, ventaja = _lider(estado.votos_tipo)
    if valor and votos >= VOTOS_TIPO and ventaja > 0:
        estado.tipo = valor


def _completo(estado):
    return bool(estado.tipo) and ejes_actuales(estado)[1]


# --- Estado visible --------------------------------------------------------------------------------

def flujo_dict(estado, ahora):
    """Etapas para la pantalla: Placa → Tipo → Ejes (cada una con su valor y si ya está lista) + pasajeros."""
    tipo = tipo_actual(estado)
    ejes, ejes_listo = ejes_actuales(estado)
    pasajeros, pasajeros_listo = pasajeros_estables(estado)
    bloqueada = bool(estado.placa)
    if not bloqueada:
        etapa = "placa"
    elif not estado.tipo:
        etapa = "tipo"
    elif not ejes_listo:
        etapa = "ejes"
    else:
        etapa = "cobro"
    return {
        "etapa": etapa,
        "placa": estado.placa or None,
        "placa_bloqueada": bloqueada,
        "tipo": tipo or None,
        "tipo_texto": TEXTO_CATEGORIA.get(tipo, tipo) or None,
        "tipo_listo": bool(estado.tipo),
        "ejes": ejes,
        "ejes_listo": ejes_listo,
        "pasajeros": pasajeros,
        "pasajeros_listo": pasajeros_listo,
        "segundos": round((ahora - estado.bloqueada_at).total_seconds(), 1) if estado.bloqueada_at else None,
    }


def flujo_de_paso(paso):
    tipo = paso.categoria_detectada
    return {
        "etapa": "registrado", "placa": paso.placa_normalizada, "placa_bloqueada": True,
        "tipo": tipo or None, "tipo_texto": paso.get_categoria_detectada_display() or None, "tipo_listo": True,
        "ejes": paso.ejes_detectados, "ejes_texto": paso.ejes_texto, "ejes_listo": True,
        "pasajeros": paso.pasajeros, "pasajeros_listo": True,
        "segundos": None,
    }


# --- Máquina de estados --------------------------------------------------------------------------

def _limpiar_captura(estado, ahora):
    estado.placa = estado.texto_leido = estado.tipo = ""
    estado.confianza = estado.bloqueada_at = None
    estado.votos_tipo, estado.votos_ejes, estado.votos_pasajeros = {}, {}, {}
    estado.evidencia = estado.evidencia_confianza = None
    estado.reiniciado_at = ahora


def _bloquear_placa(estado, voto, ahora):
    _limpiar_captura(estado, ahora)
    estado.paso = None  # la tarjeta se limpia: empieza un vehículo nuevo
    estado.placa = voto["placa"]
    estado.texto_leido = voto["placa"]
    estado.confianza = voto["confianza"]
    estado.bloqueada_at = estado.ultimo_visto_at = ahora
    # Los frames que confirmaron la placa ya votan por su tipo, ejes y pasajeros.
    for l in LecturaPlaca.objects.filter(caseta_id=estado.caseta_id, placa=voto["placa"],
                                         created_at__gte=ahora - cobro.VENTANA_VOTO):
        _sumar(estado.votos_tipo, l.categoria)
        _sumar(estado.votos_ejes, l.ejes)
        _sumar(estado.votos_pasajeros, l.pasajeros)
    _bloquear_tipo(estado)


def _acumular(estado, lecturas, vehiculo, imagen, ahora):
    """Suma los datos del frame al vehículo de la placa bloqueada."""
    propia = next((l for l in lecturas if l["placa"] == estado.placa), None)
    otra_placa = bool(lecturas) and propia is None
    hay_vehiculo = bool(vehiculo.get("caja_vehiculo") or vehiculo.get("clase"))
    if propia:
        # Tipo y ejes del vehículo que contiene la placa bloqueada; el mejor frame queda como evidencia.
        if not estado.tipo:
            _sumar(estado.votos_tipo, propia["categoria"])
        _sumar(estado.votos_ejes, propia["ejes"] if propia["ejes"] is not None else vehiculo.get("ejes"))
        _sumar(estado.votos_pasajeros, vehiculo.get("pasajeros"))
        if (propia["confianza"] or 0) >= (estado.evidencia_confianza or 0) or estado.evidencia is None:
            estado.evidencia = vehiculo.get("evidencia") or imagen
            estado.evidencia_confianza = propia["confianza"] or 0
            if (propia["confianza"] or 0) > (estado.confianza or 0):
                estado.confianza = propia["confianza"]
                estado.texto_leido = propia["texto_leido"][:20]
        estado.ultimo_visto_at = ahora
    elif hay_vehiculo and not otra_placa:
        # Placa no legible en este frame: el vehículo principal sigue siendo el del paso.
        if not estado.tipo and vehiculo.get("clase") in TEXTO_CATEGORIA:
            _sumar(estado.votos_tipo, vehiculo["clase"])
        _sumar(estado.votos_ejes, vehiculo.get("ejes"))
        _sumar(estado.votos_pasajeros, vehiculo.get("pasajeros"))
    _bloquear_tipo(estado)


def _datos_finales(estado):
    """(categoria, ejes, fuente) para cobrar: lo detectado o, si falta, los respaldos (ver docstring)."""
    if estado.tipo:
        categoria, fuente = estado.tipo, "inferencia"
    else:
        registrado = Vehiculo.objects.filter(placa=estado.placa).first()
        mas_visto = _lider(estado.votos_tipo)[0]
        if registrado:
            categoria, fuente = registrado.categoria, "registro"
        elif mas_visto:
            categoria, fuente = mas_visto, "inferencia"
        else:
            categoria, fuente = TIPO_RESPALDO, "respaldo"
    ejes, _ = ejes_actuales(estado)  # None = no se vio ningún eje: se cobran 2 (estimado)
    return categoria, ejes, fuente


def _registrar(estado, usuario, ahora, motivo):
    categoria, ejes, fuente = _datos_finales(estado)
    pasajeros, _ = pasajeros_estables(estado)
    evidencia = bytes(estado.evidencia) if estado.evidencia is not None else None
    paso, _, info = cobro.registrar_paso(
        estado.caseta_id, estado.placa, usuario=usuario, confianza=estado.confianza,
        texto_leido=estado.texto_leido or estado.placa, categoria=categoria, ejes=ejes, pasajeros=pasajeros,
        ahora=ahora, antiduplicado=False, fuente_categoria=fuente)
    _limpiar_captura(estado, ahora)
    estado.paso = paso
    estado.ultimo_visto_at = ahora
    return {"accion": "registrado", "paso": paso, "info": info, "evidencia": evidencia, "motivo": motivo,
            "flujo": flujo_de_paso(paso)}


def procesar_frame(caseta, lecturas, vehiculo, imagen, usuario=None, ahora=None):
    """Avanza la máquina de estados de la caseta con un frame ya inferido.

    Devuelve {"accion", "flujo", "paso", "info", "evidencia", "motivo", "votos", "resuelto"}; accion: sin_placa |
    esperando_confirmacion | en_curso | registrado | fijo.
    """
    ahora = ahora or timezone.now()
    with transaction.atomic():
        estado, _ = fila_bloqueada().get_or_create(caseta=caseta)
        resultado = _avanzar(estado, caseta, lecturas, vehiculo, imagen, usuario, ahora)
        # Solo si todo salió bien: si _avanzar falla, la transacción se deshace y el error original queda en el log
        # (un save() en un finally lo taparía en PostgreSQL con "current transaction is aborted").
        estado.ultimo_frame_at = ahora
        estado.save()
        return resultado


def fila_bloqueada():
    """Consulta que bloquea la fila de la caseta (serializa sus frames).

    Sin select_related("paso"): `paso` es una FK nullable y PostgreSQL rechaza FOR UPDATE sobre un LEFT OUTER JOIN
    ("FOR UPDATE cannot be applied to the nullable side of an outer join"). El paso fijo se carga aparte, sin bloqueo.
    """
    return PasoEnCurso.objects.select_for_update(of=("self",))


def _avanzar(estado, caseta, lecturas, vehiculo, imagen, usuario, ahora):
    if estado.placa:  # CAPTURA
        _acumular(estado, lecturas, vehiculo, imagen, ahora)
        return _cerrar_si_listo(estado, usuario, ahora) or {"accion": "en_curso", "flujo": flujo_dict(estado, ahora)}

    fijo = estado.paso  # RESULTADO FIJO (o None = espera)
    vigente = fijo is not None and (estado.ultimo_visto_at is None or ahora - estado.ultimo_visto_at < REPETIR)
    # Tras 10 min sin verla, la placa del resultado fijo vuelve a contar como vehículo nuevo (la pantalla sigue igual).
    excluida = fijo.placa_normalizada if vigente else None
    if excluida and any(l["placa"] == excluida for l in lecturas):
        estado.ultimo_visto_at = ahora  # sigue el mismo vehículo
    nuevas = [l for l in lecturas if l["placa"] != excluida]
    mejor = nuevas[0] if nuevas else None
    if mejor is None:
        return _resultado_fijo(fijo) if fijo else {"accion": "sin_placa", "flujo": flujo_dict(estado, ahora)}

    LecturaPlaca.objects.create(caseta=caseta, placa=mejor["placa"], confianza=mejor["confianza"],
                                categoria=mejor["categoria"], ejes=mejor["ejes"], pasajeros=vehiculo.get("pasajeros"),
                                created_at=ahora)
    voto = cobro.votar(caseta, ahora, desde=estado.reiniciado_at, excluir=excluida)
    if voto is None:
        votos = LecturaPlaca.objects.filter(caseta=caseta, placa=mejor["placa"],
                                            created_at__gte=_inicio_votacion(estado, ahora)).count()
        if fijo:  # una lectura suelta de otra placa no cambia la pantalla
            return {**_resultado_fijo(fijo), "votos": votos, "candidata": mejor["placa"]}
        return {"accion": "esperando_confirmacion", "votos": votos, "flujo": flujo_dict(estado, ahora)}

    # Placa distinta confirmada: vehículo nuevo. Si el anterior quedó pendiente de pago, pagó en efectivo al irse.
    anterior = estado.paso
    resuelto = anterior if anterior and cobro.cobrar_en_efectivo(anterior, cobro.NOTA_SIGUIENTE, ahora) else None
    _bloquear_placa(estado, voto, ahora)
    if mejor["placa"] == estado.placa:
        estado.evidencia = vehiculo.get("evidencia") or imagen
        estado.evidencia_confianza = mejor["confianza"] or 0
        estado.texto_leido = (mejor["texto_leido"] or mejor["placa"])[:20]
    cobro.limpiar_lecturas_viejas()
    r = (_cerrar_si_listo(estado, usuario, ahora)
         or {"accion": "en_curso", "flujo": flujo_dict(estado, ahora), "votos": voto["votos"]})
    if resuelto:
        r["resuelto"] = resuelto
    return r


def _cerrar_si_listo(estado, usuario, ahora):
    if _completo(estado):
        return _registrar(estado, usuario, ahora, "completo")
    if ahora - estado.bloqueada_at >= MAX_CAPTURA:
        return _registrar(estado, usuario, ahora, "tiempo")
    return None


def _resultado_fijo(paso):
    return {"accion": "fijo", "paso": paso, "flujo": flujo_de_paso(paso)}


def _inicio_votacion(estado, ahora):
    inicio = ahora - cobro.VENTANA_VOTO
    return max(inicio, estado.reiniciado_at) if estado.reiniciado_at else inicio


def estado_de(caseta):
    """Flujo actual de la caseta (para pintar la pantalla al cargar)."""
    estado = PasoEnCurso.objects.select_related("paso").filter(caseta=caseta).first()
    if estado is None:
        return flujo_dict(PasoEnCurso(caseta=caseta), timezone.now())
    if not estado.placa and estado.paso:
        return flujo_de_paso(estado.paso)
    return flujo_dict(estado, timezone.now())
