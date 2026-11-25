"""Flujo automático del punto de cobro: UN VEHÍCULO = UN COBRO.

Máquina de estados por caseta (fila PasoEnCurso), alimentada por los frames de la cámara:

  ESPERA      Sin vehículo. Cada lectura de placa vota (2 lecturas iguales en 4 s o 1 con confianza >= 0.80).
  CAPTURA     Placa confirmada y BLOQUEADA: no cambia aunque otros frames lean otra cosa. El TIPO (M2) no se
              decide al primer frame: desde la primera detección del vehículo se OBSERVA 1.5 s sumando la
              confianza de cada clase en cada frame (mayoría ponderada), y recién ahí se bloquea y ya no cambia.
              Los EJES (M3) igual: 1.5 s desde su primera detección, por mayoría de frames. Los PASAJEROS (M4) se
              acumulan por frame. Con tipo y ejes bloqueados, a los 6 s del bloqueo de la placa, o si el vehículo
              SALE del cuadro (2 frames seguidos sin placa ni vehículo), se registra y cobra UNA sola vez.
              Si la observación no terminó: tipo = clase con mayor suma de confianza entre los frames vistos (con
              un solo frame, esa clase) y ejes = el último conteo observado. Solo si M2 nunca vio el vehículo: la
              categoría del vehículo registrado o, si no está registrado, "auto". Sin ejes observados: 2 (mínimo
              de la categoría), marcados como estimados (ejes_detectados = null).
  RESULTADO   El paso registrado queda FIJO. Mientras se vea la misma placa o el vehículo, no se recalcula ni se
  FIJO        registra nada. Solo una placa DISTINTA confirmada por votación inicia un vehículo nuevo.
  SALIDA      El vehículo SALIÓ cuando no se ve ni su placa ni ningún vehículo (M2) en 2 frames seguidos y pasaron
              >= 3 s desde la última vez que se vio. Al salir, si su paso quedó "pendiente_pago" (no registrada o
              saldo insuficiente) se cobra en efectivo (cobro.cobrar_en_efectivo, nota "al salir el vehículo"); si ya
              estaba cobrado con saldo no cambia nada. Si una placa nueva se confirma antes de que el anterior salga
              del todo, primero se cierra el anterior igual (salió) y luego empieza el nuevo. Al cerrar el punto de
              cobro (cerrar_punto) el pendiente también se da por cobrado.
              La misma placa vuelve a cobrarse solo después de otra placa distinta o tras 10 min sin verla.
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import cobro
from .models import CATEGORIAS, LecturaPlaca, PasoEnCurso, Vehiculo

MAX_CAPTURA = timedelta(seconds=6)    # tope para juntar tipo y ejes después de bloquear la placa
OBSERVACION = timedelta(seconds=1.5)  # ventana de observación del tipo y de los ejes desde su primera detección
REPETIR = timedelta(minutes=10)       # la misma placa sin verse este tiempo vuelve a ser un vehículo nuevo
SALIDA = timedelta(seconds=3)         # sin ver la placa ni ningún vehículo este tiempo (y FRAMES_SALIDA frames) = salió
FRAMES_SALIDA = 2
VOTOS_PASAJEROS = 2
PESO_SIN_CONFIANZA = 0.5  # peso del voto de tipo si M2 no informó su confianza
TIPO_RESPALDO = "auto"
TEXTO_CATEGORIA = dict(CATEGORIAS)


# --- Conteos -------------------------------------------------------------------------------------

def _sumar(votos, valor, peso=1):
    if valor is None or valor == "":
        return
    clave = str(valor)
    votos[clave] = round(votos.get(clave, 0) + peso, 4)


def _lider(votos):
    """(valor, votos) del más votado; a igualdad, el mayor valor."""
    if not votos:
        return None, 0
    return max(votos.items(), key=lambda kv: (kv[1], _num(kv[0])))


def _num(clave):
    try:
        return int(clave)
    except (TypeError, ValueError):
        return clave


def tipo_actual(estado):
    """Tipo bloqueado o, mientras se observa, el provisional (mayor suma de confianza hasta ahora)."""
    return estado.tipo or (_lider(estado.votos_tipo)[0] or "")


def ejes_actuales(estado):
    """(ejes, bloqueados): los bloqueados o, mientras se observan, el último conteo visto."""
    if estado.ejes is not None:
        return estado.ejes, True
    return estado.ultimo_eje, False


def pasajeros_estables(estado):
    """Máximo conteo visto en >= 2 frames; (valor, estable). Sin conteo estable, el más repetido."""
    estables = [int(k) for k, n in estado.votos_pasajeros.items() if n >= VOTOS_PASAJEROS]
    if estables:
        return max(estables), True
    valor = _lider(estado.votos_pasajeros)[0]
    return (int(valor) if valor is not None else None), False


def _en_ventana(desde, cuando):
    return desde is None or cuando - desde <= OBSERVACION


def _observar_tipo(estado, clase, confianza, cuando):
    """Suma la confianza de la clase M2 de ESTE vehículo mientras dura su observación de 1.5 s."""
    if estado.tipo or clase not in TEXTO_CATEGORIA or not _en_ventana(estado.tipo_desde, cuando):
        return
    estado.tipo_desde = estado.tipo_desde or cuando
    _sumar(estado.votos_tipo, clase, confianza if confianza is not None else PESO_SIN_CONFIANZA)


def _observar_ejes(estado, ejes, cuando):
    if estado.ejes is not None or ejes is None:
        return
    estado.ultimo_eje = ejes
    if _en_ventana(estado.ejes_desde, cuando):
        estado.ejes_desde = estado.ejes_desde or cuando
        _sumar(estado.votos_ejes, ejes)


def _cerrar_observacion(estado, ahora):
    """Cumplidos 1.5 s desde la primera detección, el tipo (mayor suma de confianza) y los ejes (mayoría de
    frames) quedan bloqueados para el resto del paso."""
    if not estado.tipo and estado.tipo_desde and ahora - estado.tipo_desde >= OBSERVACION:
        estado.tipo = _lider(estado.votos_tipo)[0] or ""
    if estado.ejes is None and estado.ejes_desde and ahora - estado.ejes_desde >= OBSERVACION:
        valor = _lider(estado.votos_ejes)[0]
        estado.ejes = int(valor) if valor is not None else estado.ultimo_eje


def _completo(estado):
    return bool(estado.tipo) and estado.ejes is not None


# --- Estado visible --------------------------------------------------------------------------------

def flujo_dict(estado, ahora):
    """Etapas para la pantalla: Placa → Tipo → Ejes (cada una con su valor y si ya está lista) + pasajeros."""
    tipo = tipo_actual(estado)
    ejes, ejes_listo = ejes_actuales(estado)
    pasajeros, pasajeros_listo = pasajeros_estables(estado)
    bloqueada = bool(estado.placa)
    analizando = bloqueada and not estado.tipo and estado.tipo_desde is not None
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
        # Observando el tipo (1.5 s): la pantalla muestra "Analizando…" con el valor provisional en gris.
        "tipo_analizando": analizando,
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
        "tipo_analizando": False,
        "ejes": paso.ejes_detectados, "ejes_texto": paso.ejes_texto, "ejes_listo": True,
        "pasajeros": paso.pasajeros, "pasajeros_listo": True,
        "segundos": None,
    }


# --- Máquina de estados --------------------------------------------------------------------------

def _limpiar_captura(estado, ahora):
    estado.placa = estado.texto_leido = estado.tipo = ""
    estado.confianza = estado.bloqueada_at = None
    estado.tipo_desde = estado.ejes = estado.ejes_desde = estado.ultimo_eje = None
    estado.votos_tipo, estado.votos_ejes, estado.votos_pasajeros = {}, {}, {}
    estado.evidencia = estado.evidencia_confianza = None
    estado.reiniciado_at = ahora


def _bloquear_placa(estado, voto, ahora):
    _limpiar_captura(estado, ahora)
    estado.paso = None  # la tarjeta se limpia: empieza un vehículo nuevo
    estado.salida_at, estado.frames_sin_vehiculo = None, 0
    estado.placa = voto["placa"]
    estado.texto_leido = voto["placa"]
    estado.confianza = voto["confianza"]
    estado.bloqueada_at = estado.ultimo_visto_at = ahora
    # Los frames que confirmaron la placa ya son parte de la observación (la ventana empieza en el primero que vio
    # el vehículo) y votan por sus pasajeros.
    for l in LecturaPlaca.objects.filter(caseta_id=estado.caseta_id, placa=voto["placa"],
                                         created_at__gte=ahora - cobro.VENTANA_VOTO).order_by("created_at"):
        _observar_tipo(estado, l.categoria, l.confianza_categoria, l.created_at)
        _observar_ejes(estado, l.ejes, l.created_at)
        _sumar(estado.votos_pasajeros, l.pasajeros)
    _cerrar_observacion(estado, ahora)


def _acumular(estado, lecturas, vehiculo, imagen, ahora):
    """Suma los datos del frame al vehículo de la placa bloqueada."""
    propia = next((l for l in lecturas if l["placa"] == estado.placa), None)
    otra_placa = bool(lecturas) and propia is None
    hay_vehiculo = bool(vehiculo.get("caja_vehiculo") or vehiculo.get("clase"))
    if propia:
        # Tipo y ejes del vehículo que contiene la placa bloqueada; el mejor frame queda como evidencia.
        _observar_tipo(estado, propia["categoria"], propia.get("confianza_clase"), ahora)
        _observar_ejes(estado, propia["ejes"] if propia["ejes"] is not None else vehiculo.get("ejes"), ahora)
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
        _observar_tipo(estado, vehiculo.get("clase"), vehiculo.get("confianza_clase"), ahora)
        _observar_ejes(estado, vehiculo.get("ejes"), ahora)
        _sumar(estado.votos_pasajeros, vehiculo.get("pasajeros"))
        estado.ultimo_visto_at = ahora
    # Salida durante la captura: frames seguidos sin ninguna placa ni vehículo.
    estado.frames_sin_vehiculo = 0 if (lecturas or hay_vehiculo) else estado.frames_sin_vehiculo + 1
    _cerrar_observacion(estado, ahora)


def _datos_finales(estado):
    """(categoria, ejes, fuente) para cobrar: lo observado o, si M2 nunca vio el vehículo, los respaldos."""
    observado = estado.tipo or _lider(estado.votos_tipo)[0]  # bloqueado o, si no terminó, mayor suma de confianza
    if observado:
        categoria, fuente = observado, "inferencia"
    else:
        registrado = Vehiculo.objects.filter(placa=estado.placa).first()
        if registrado:
            categoria, fuente = registrado.categoria, "registro"
        else:
            categoria, fuente = TIPO_RESPALDO, "respaldo"
    ejes, _ = ejes_actuales(estado)  # bloqueados o el último visto; None = ningún eje: se cobran 2 (estimado)
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
    estado.salida_at = None
    if motivo != "salida":  # si ya salió, se conservan su última vista y los frames vacíos para darle salida
        estado.ultimo_visto_at, estado.frames_sin_vehiculo = ahora, 0
    return {"accion": "registrado", "paso": paso, "info": info, "evidencia": evidencia, "motivo": motivo,
            "flujo": flujo_de_paso(paso)}


def procesar_frame(caseta, lecturas, vehiculo, imagen, usuario=None, ahora=None):
    """Avanza la máquina de estados de la caseta con un frame ya inferido.

    Devuelve {"accion", "flujo", "paso", "info", "evidencia", "motivo", "votos", "resuelto"}; accion: sin_placa |
    esperando_confirmacion | en_curso | registrado | fijo | salida. `resuelto`: paso pendiente que se acaba de
    cobrar en efectivo (para avisar por Pusher).
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
    en_pantalla = fijo is not None and estado.salida_at is None  # su vehículo aún no salió
    vigente = fijo is not None and (estado.ultimo_visto_at is None or ahora - estado.ultimo_visto_at < REPETIR)
    # Tras 10 min sin verla, la placa del resultado fijo vuelve a contar como vehículo nuevo.
    excluida = fijo.placa_normalizada if vigente else None
    propia = bool(excluida) and any(l["placa"] == excluida for l in lecturas)
    if propia:
        estado.ultimo_visto_at = ahora  # sigue el mismo vehículo
        if not en_pantalla:  # se volvió a ver tras darlo por salido: vuelve su resultado fijo (ya está cobrado)
            estado.salida_at, en_pantalla = None, True
    nuevas = [l for l in lecturas if l["placa"] != excluida]
    mejor = nuevas[0] if nuevas else None

    salio = None
    if en_pantalla:
        if propia or nuevas or vehiculo.get("caja_vehiculo") or vehiculo.get("clase"):
            estado.frames_sin_vehiculo = 0
            estado.ultimo_visto_at = ahora
        else:
            estado.frames_sin_vehiculo += 1
            if estado.frames_sin_vehiculo >= FRAMES_SALIDA and ahora - estado.ultimo_visto_at >= SALIDA:
                salio = _dar_salida(estado, ahora, cobro.NOTA_SALIDA)
    if mejor is None:
        if salio:
            return salio
        return _resultado_fijo(fijo) if en_pantalla else {"accion": "sin_placa", "flujo": flujo_dict(estado, ahora)}

    LecturaPlaca.objects.create(caseta=caseta, placa=mejor["placa"], confianza=mejor["confianza"],
                                categoria=mejor["categoria"], confianza_categoria=mejor.get("confianza_clase"),
                                ejes=mejor["ejes"], pasajeros=vehiculo.get("pasajeros"), created_at=ahora)
    voto = cobro.votar(caseta, ahora, desde=estado.reiniciado_at, excluir=excluida)
    if voto is None:
        votos = LecturaPlaca.objects.filter(caseta=caseta, placa=mejor["placa"],
                                            created_at__gte=_inicio_votacion(estado, ahora)).count()
        if en_pantalla:  # una lectura suelta de otra placa no cambia la pantalla
            return {**_resultado_fijo(fijo), "votos": votos, "candidata": mejor["placa"]}
        return {"accion": "esperando_confirmacion", "votos": votos, "flujo": flujo_dict(estado, ahora)}

    # Placa distinta confirmada: vehículo nuevo. Si el anterior no terminó de salir, primero se cierra (salió).
    resuelto = _dar_salida(estado, ahora, cobro.NOTA_SALIDA).get("resuelto") if en_pantalla else None
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


def _dar_salida(estado, ahora, nota):
    """El vehículo del resultado fijo salió: si su paso estaba pendiente de pago, se cobra en efectivo."""
    paso = estado.paso
    cobrado_ahora = cobro.cobrar_en_efectivo(paso, nota, ahora)
    estado.salida_at, estado.frames_sin_vehiculo = ahora, 0
    return {"accion": "salida", "paso": paso, "flujo": flujo_de_paso(paso),
            "resuelto": paso if cobrado_ahora else None}


def cerrar_punto(caseta, ahora=None):
    """El operador detuvo el punto de cobro: el vehículo en pantalla se da por salido y, si su paso estaba
    pendiente de pago, por cobrado ("Cobrado al cerrar el punto de cobro"). Devuelve el paso cobrado o None."""
    ahora = ahora or timezone.now()
    with transaction.atomic():
        estado = fila_bloqueada().filter(caseta=caseta).first()
        if estado is None or estado.paso_id is None or estado.salida_at is not None:
            return None
        r = _dar_salida(estado, ahora, cobro.NOTA_CIERRE)
        estado.save(update_fields=["salida_at", "frames_sin_vehiculo"])
        return r["resuelto"]


def _cerrar_si_listo(estado, usuario, ahora):
    if _completo(estado):
        return _registrar(estado, usuario, ahora, "completo")
    if ahora - estado.bloqueada_at >= MAX_CAPTURA:
        return _registrar(estado, usuario, ahora, "tiempo")
    if estado.frames_sin_vehiculo >= FRAMES_SALIDA:  # salió antes de cerrar la observación: con lo observado
        return _registrar(estado, usuario, ahora, "salida")
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
    if not estado.placa and estado.paso and estado.salida_at is None:
        return flujo_de_paso(estado.paso)
    return flujo_dict(estado, timezone.now())
