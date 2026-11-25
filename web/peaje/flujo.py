"""Flujo automático del punto de cobro: un vehículo = un paso.

Máquina de estados por caseta (fila PasoEnCurso), alimentada por los frames de la cámara:

  a) PLACA (M1): se confirma por votación (2 lecturas iguales en 4 s o 1 con confianza >= 0.80) y se BLOQUEA:
     no cambia durante el paso aunque otros frames lean otra cosa. Se guarda el mejor frame de esa placa.
  b) TIPO (M2): clase del vehículo que contiene la placa bloqueada; mayoría entre los frames del paso; se bloquea
     cuando una clase suma 2 votos y supera a las demás.
  c) EJES (M3): mayoría entre los frames del paso (mínimo 2 al cobrar).
  d) PASAJEROS (M4): máximo conteo estable (visto en >= 2 frames).
  e) COBRO: con placa + tipo + ejes, al salir de cuadro (sin vehículo durante 2 s) o a los 8 s del bloqueo, se
     registra solo. Si el vehículo sale sin tipo/ejes también se registra (con los respaldos de cobro.registrar_paso)
     para que nadie pase sin paso; y a los 15 s se registra siempre. Luego se reinicia para el siguiente vehículo.
     Anti-duplicado: la misma placa en la misma caseta no se registra otra vez en 30 s.
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import cobro
from .models import CATEGORIAS, LecturaPlaca, PasoEnCurso

SALIDA = timedelta(seconds=2)       # sin ver el vehículo este tiempo = salió de cuadro
ESPERA_COBRO = timedelta(seconds=8)  # con los datos completos, se cobra a los 8 s del bloqueo aunque siga en cuadro
LIMITE = timedelta(seconds=15)       # se registra siempre, aunque falten datos
VOTOS_TIPO = 2
VOTOS_EJES = 2
VOTOS_PASAJEROS = 2
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


# --- Estado visible --------------------------------------------------------------------------------

def flujo_dict(estado, ahora):
    """Etapas para la pantalla: Placa → Tipo → Ejes → Pasajeros (cada una con su valor y si ya está lista)."""
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
    elif not pasajeros_listo:
        etapa = "pasajeros"
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
        "ejes": paso.ejes_detectados, "ejes_listo": True, "pasajeros": paso.pasajeros, "pasajeros_listo": True,
        "segundos": None,
    }


# --- Máquina de estados --------------------------------------------------------------------------

def _reiniciar(estado, ahora):
    estado.placa = estado.texto_leido = estado.tipo = ""
    estado.confianza = estado.bloqueada_at = estado.ultimo_visto_at = None
    estado.votos_tipo, estado.votos_ejes, estado.votos_pasajeros = {}, {}, {}
    estado.evidencia = estado.evidencia_confianza = None
    estado.reiniciado_at = ahora


def _bloquear_placa(estado, voto, ahora):
    estado.placa = voto["placa"]
    estado.texto_leido = voto["placa"]
    estado.confianza = voto["confianza"]
    estado.bloqueada_at = estado.ultimo_visto_at = ahora
    # Los frames que confirmaron la placa ya votan por su tipo, ejes y pasajeros.
    for l in LecturaPlaca.objects.filter(caseta_id=estado.caseta_id, placa=voto["placa"],
                                         created_at__gte=_inicio_votacion(estado, ahora)):
        _sumar(estado.votos_tipo, l.categoria)
        _sumar(estado.votos_ejes, l.ejes)
        _sumar(estado.votos_pasajeros, l.pasajeros)
    _bloquear_tipo(estado)


def _acumular(estado, lecturas, vehiculo, imagen, ahora):
    """Suma los datos del frame al vehículo de la placa bloqueada. Devuelve True si el vehículo está en cuadro."""
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
    elif hay_vehiculo and not otra_placa:
        # Placa no legible en este frame: el vehículo principal sigue siendo el del paso.
        if not estado.tipo and vehiculo.get("clase") in TEXTO_CATEGORIA:
            _sumar(estado.votos_tipo, vehiculo["clase"])
        _sumar(estado.votos_ejes, vehiculo.get("ejes"))
        _sumar(estado.votos_pasajeros, vehiculo.get("pasajeros"))
    _bloquear_tipo(estado)
    visto = bool(propia) or (hay_vehiculo and not otra_placa)
    if visto:
        estado.ultimo_visto_at = ahora
    return visto


def _listo_para_cobrar(estado, ahora):
    """'salida' | 'tiempo' | 'limite' | None."""
    if ahora - estado.ultimo_visto_at >= SALIDA:
        return "salida"
    transcurrido = ahora - estado.bloqueada_at
    if transcurrido >= LIMITE:
        return "limite"
    if transcurrido >= ESPERA_COBRO and estado.tipo and ejes_actuales(estado)[1]:
        return "tiempo"
    return None


def _registrar(estado, usuario, ahora, motivo):
    categoria = tipo_actual(estado)
    ejes, _ = ejes_actuales(estado)
    pasajeros, _ = pasajeros_estables(estado)
    evidencia = bytes(estado.evidencia) if estado.evidencia is not None else None
    paso, duplicado, info = cobro.registrar_paso(
        estado.caseta_id, estado.placa, usuario=usuario, confianza=estado.confianza,
        texto_leido=estado.texto_leido or estado.placa, categoria=categoria, ejes=ejes, pasajeros=pasajeros,
        ahora=ahora)
    _reiniciar(estado, ahora)
    return {"accion": "duplicado" if duplicado else "registrado", "paso": paso, "info": info,
            "evidencia": None if duplicado else evidencia, "motivo": motivo, "flujo": flujo_de_paso(paso)}


def procesar_frame(caseta, lecturas, vehiculo, imagen, usuario=None, ahora=None):
    """Avanza la máquina de estados de la caseta con un frame ya inferido.

    Devuelve {"accion", "flujo", "paso", "info", "evidencia", "motivo", "votos"}; accion: sin_placa |
    esperando_confirmacion | en_curso | registrado | duplicado.
    """
    ahora = ahora or timezone.now()
    with transaction.atomic():
        estado, _ = PasoEnCurso.objects.select_for_update().get_or_create(caseta=caseta)
        try:
            return _avanzar(estado, caseta, lecturas, vehiculo, imagen, usuario, ahora)
        finally:
            estado.save()


def _avanzar(estado, caseta, lecturas, vehiculo, imagen, usuario, ahora):
    # La cámara estuvo detenida: el vehículo pendiente ya no está; se registra con lo que se juntó.
    pausa = estado.ultimo_frame_at is not None and ahora - estado.ultimo_frame_at >= SALIDA
    estado.ultimo_frame_at = ahora
    if estado.placa and pausa:
        return _registrar(estado, usuario, ahora, "salida")

    if estado.placa:
        _acumular(estado, lecturas, vehiculo, imagen, ahora)
        motivo = _listo_para_cobrar(estado, ahora)
        if motivo:
            return _registrar(estado, usuario, ahora, motivo)
        return {"accion": "en_curso", "flujo": flujo_dict(estado, ahora)}

    mejor = lecturas[0] if lecturas else None
    if mejor is None:
        return {"accion": "sin_placa", "flujo": flujo_dict(estado, ahora)}
    LecturaPlaca.objects.create(caseta=caseta, placa=mejor["placa"], confianza=mejor["confianza"],
                                categoria=mejor["categoria"], ejes=mejor["ejes"], pasajeros=vehiculo.get("pasajeros"),
                                created_at=ahora)
    previo = cobro.paso_reciente(caseta, mejor["placa"], ahora)
    if previo:
        return {"accion": "duplicado", "paso": previo, "flujo": flujo_dict(estado, ahora)}
    voto = cobro.votar(caseta, ahora, desde=estado.reiniciado_at)
    if voto is None or cobro.paso_reciente(caseta, voto["placa"], ahora):
        votos = LecturaPlaca.objects.filter(caseta=caseta, placa=mejor["placa"],
                                            created_at__gte=_inicio_votacion(estado, ahora)).count()
        return {"accion": "esperando_confirmacion", "votos": votos, "flujo": flujo_dict(estado, ahora)}

    _bloquear_placa(estado, voto, ahora)
    if mejor["placa"] == estado.placa:
        estado.evidencia = vehiculo.get("evidencia") or imagen
        estado.evidencia_confianza = mejor["confianza"] or 0
        estado.texto_leido = (mejor["texto_leido"] or mejor["placa"])[:20]
    cobro.limpiar_lecturas_viejas()
    return {"accion": "en_curso", "flujo": flujo_dict(estado, ahora), "votos": voto["votos"]}


def _inicio_votacion(estado, ahora):
    inicio = ahora - cobro.VENTANA_VOTO
    return max(inicio, estado.reiniciado_at) if estado.reiniciado_at else inicio


def estado_de(caseta):
    """Flujo actual de la caseta (para pintar la pantalla al cargar)."""
    estado = PasoEnCurso.objects.filter(caseta=caseta).first()
    if estado is None:
        return flujo_dict(PasoEnCurso(caseta=caseta), timezone.now())
    return flujo_dict(estado, timezone.now())

