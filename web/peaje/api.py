"""API v1. Autenticación: JWT (app móvil) o sesión web + CSRF (modo caseta y consulta en el navegador)."""
import base64
import logging
import time

from django.conf import settings
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from . import cobro, flujo, panel
from .inferencia import InferenciaNoDisponible, inferir
from .models import Caseta

log = logging.getLogger("peaje.caseta")

_respuesta = inline_serializer("RespuestaLibre", fields={"detalle": serializers.CharField(required=False)})


def _imagen(request):
    """(bytes, None) o (None, Response de error)."""
    archivo = request.FILES.get("imagen")
    if archivo is None:
        return None, Response({"detalle": "Falta el archivo 'imagen'."}, status=status.HTTP_400_BAD_REQUEST)
    if archivo.size > settings.IMAGEN_MAX_BYTES:
        return None, Response({"detalle": "La imagen es demasiado grande."},
                              status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    return archivo.read(), None


def _ms(t0):
    return round((time.perf_counter() - t0) * 1000)


@extend_schema(
    summary="Procesa un frame de la cámara de una caseta",
    description=(
        "Un vehículo = un cobro. Cada frame alimenta la máquina de estados de la caseta: (a) PLACA por votación "
        "(2 lecturas iguales en 4 s o 1 con confianza >= 0.80) que queda bloqueada; (b) TIPO por mayoría sobre el "
        "vehículo de esa placa; (c) EJES por mayoría; (d) PASAJEROS: máximo conteo estable. (e) Con tipo y ejes, o "
        "a los 6 s del bloqueo (con respaldos: tipo del registro, clase más vista o 'auto'; 2 ejes estimados), se "
        "registra y cobra una sola vez. (f) El resultado queda FIJO (`accion: fijo`) mientras se vea la misma placa "
        "o ninguna; solo una placa distinta confirmada inicia otro vehículo. La misma placa vuelve a cobrarse tras "
        "otra placa distinta o 10 min sin verla. "
        "Respuesta: `accion` `sin_placa` | `esperando_confirmacion` | `en_curso` | `registrado` | `fijo` | "
        "`error`; `flujo` {etapa, placa, placa_bloqueada, tipo, tipo_texto, tipo_listo, ejes, ejes_listo, pasajeros, "
        "pasajeros_listo, segundos}; `lectura` (resultado de ESTE frame: placa, confianza, categoria, ejes, "
        "pasajeros, cajas [x1, y1, x2, y2] en píxeles del frame enviado, latencias); `paso` (si se registró o es el "
        "fijo), `mensaje` (texto corto en español), `captura` (evidencia del paso registrado). Si la "
        "inferencia falla responde 502 con `accion: error`."
    ),
    request=inline_serializer("FrameEntrada", fields={
        "imagen": serializers.ImageField(), "caseta_id": serializers.IntegerField()}),
    responses={200: _respuesta, 400: _respuesta, 502: _respuesta},
)
@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
def paso_frame(request):
    # Nunca un 500 genérico de Django: el error queda en el log con traceback y el cliente recibe JSON.
    try:
        return _paso_frame(request)
    except Exception:  # noqa: BLE001
        log.exception("frame: error inesperado (caseta_id=%s)", request.data.get("caseta_id"))
        return Response({"accion": "error", "mensaje": "Error interno del servidor al procesar el frame.",
                         "lectura": None, "paso": None}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def _paso_frame(request):
    t0 = time.perf_counter()
    cid = str(request.data.get("caseta_id", ""))
    caseta = Caseta.objects.filter(pk=int(cid)).first() if cid.isdigit() else None
    if caseta is None:
        return Response({"detalle": "caseta_id inválido."}, status=status.HTTP_400_BAD_REQUEST)
    imagen, error = _imagen(request)
    if error:
        return error

    def responder(accion, mensaje, lectura, codigo=status.HTTP_200_OK, **extra):
        ms = _ms(t0)
        if lectura is not None:
            lectura["latencia_ms"] = ms
        log.info("frame caseta=%s accion=%s placa=%s categoria=%s %d ms%s", caseta.id, accion,
                 (lectura or {}).get("placa") or "-", (lectura or {}).get("categoria") or "-", ms,
                 f" · {mensaje}" if accion == "error" else "")
        return Response({"accion": accion, "mensaje": mensaje, "lectura": lectura, "paso": None,
                         "latencia_ms": ms, **extra}, status=codigo)

    try:
        lecturas, ms_inferencia, vehiculo = inferir(imagen)
    except InferenciaNoDisponible as e:
        return responder("error", f"Sin conexión con la inferencia: {e}", None,
                         codigo=status.HTTP_502_BAD_GATEWAY, detalle=f"Inferencia no disponible: {e}")
    except Exception:  # noqa: BLE001 - nunca fallar en silencio: el cliente muestra el error y sigue
        log.exception("frame caseta=%s: fallo inesperado al interpretar la inferencia", caseta.id)
        return responder("error", "Error al procesar la respuesta de la inferencia.", None,
                         codigo=status.HTTP_502_BAD_GATEWAY, detalle="Respuesta de inferencia inválida.")

    mejor = lecturas[0] if lecturas else None
    # Primero el vehículo apuntado (el que contiene la placa leída; sin placa, el elegido por el servidor) y
    # después SU clase, tal cual modelo.names. Nunca la detección con más porcentaje de la imagen.
    apuntado = mejor if mejor else vehiculo
    lectura = {
        "placa": mejor["placa"] if mejor else None,
        "confianza": mejor["confianza"] if mejor else None,
        "categoria": apuntado.get("clase") or "",
        "confianza_categoria": apuntado.get("confianza_clase"),
        "caja_placa": mejor.get("caja") if mejor else None,
        "caja_vehiculo": apuntado.get("caja_vehiculo"),
        "cajas_placas": vehiculo.get("cajas_placas") or [],
        "cajas_llantas": vehiculo.get("cajas_llantas") or [],
        "ejes": vehiculo.get("ejes"),
        "llantas_detectadas": vehiculo.get("llantas_detectadas"),
        "pasajeros": vehiculo.get("pasajeros"),
        "detalle_ejes": vehiculo.get("detalle_ejes") or "",
        "latencia_inferencia_ms": ms_inferencia,
        "latencia_modelos_ms": vehiculo.get("latencias") or {},
    }
    r = flujo.procesar_frame(caseta, lecturas, vehiculo, imagen, usuario=request.user)
    accion, estado_flujo = r["accion"], r["flujo"]
    if accion == "sin_placa":
        return responder("sin_placa", "Buscando placa…", lectura, flujo=estado_flujo)
    if accion == "esperando_confirmacion":
        return responder("esperando_confirmacion",
                         f"Leyendo {mejor['placa']} (confirmando {r['votos']}/{cobro.MIN_VOTOS})", lectura,
                         flujo=estado_flujo, votos=r["votos"], votos_necesarios=cobro.MIN_VOTOS)
    if accion == "fijo":
        # Mismo vehículo (o ninguno): no se recalcula nada; se repite el resultado fijo.
        return responder("fijo", _mensaje_cobro(r["paso"]), lectura, flujo=estado_flujo,
                         paso=cobro.paso_a_dict(r["paso"]), candidata=r.get("candidata"))
    if accion == "en_curso":
        return responder("en_curso", MENSAJE_ETAPA[estado_flujo["etapa"]].format(**estado_flujo), lectura,
                         flujo=estado_flujo)

    # registrado: ya hay tipo y ejes o se cumplieron los 6 s; el paso se cobró solo (una vez).
    paso, evidencia = r["paso"], r["evidencia"]
    panel.publicar_paso(paso, evidencia)  # captura + evento en tiempo real
    return responder("registrado", _mensaje_cobro(paso), lectura, flujo=estado_flujo,
                     paso=cobro.paso_a_dict(paso, r["info"]), motivo=r["motivo"],
                     captura=("data:image/jpeg;base64," + base64.b64encode(evidencia).decode()) if evidencia else None)


MENSAJE_ETAPA = {
    "placa": "Buscando placa…",
    "tipo": "Placa {placa} · detectando tipo",
    "ejes": "Placa {placa} · contando ejes",
    "cobro": "Placa {placa} · registrando cobro",
}


def _mensaje_cobro(paso):
    """"Tarifa: X Bs · <resultado>" (la tarifa se muestra siempre, se haya cobrado o no)."""
    tarifa = f"Tarifa: {paso.tarifa:.2f} Bs" if paso.tarifa is not None else "Tarifa: –"
    return f"{tarifa} · {paso.resultado_texto}"


@extend_schema(
    summary="Consulta el estado de una placa",
    description=("Estados: `habilitada`, `saldo_insuficiente`, `inactiva`, `no_registrada` (+ sugerencias). "
                 "`ultimo_paso`: último paso registrado de la placa (hora, estado, captura) o null."),
    parameters=[OpenApiParameter("placa", str, required=True)],
    responses={200: _respuesta, 400: _respuesta},
)
@api_view(["GET"])
def vehiculo_consulta(request):
    if not (request.query_params.get("placa") or "").strip():
        return Response({"detalle": "Falta el parámetro 'placa'."}, status=status.HTTP_400_BAD_REQUEST)
    return Response(cobro.consultar_placa(request.query_params["placa"]))


@extend_schema(
    summary="Lee la placa de una foto y consulta su estado",
    request=inline_serializer("FotoEntrada", fields={"imagen": serializers.ImageField()}),
    responses={200: _respuesta, 400: _respuesta, 502: _respuesta},
)
@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
def placa_leer(request):
    t0 = time.perf_counter()
    imagen, error = _imagen(request)
    if error:
        return error
    try:
        lecturas, ms_inferencia, _ = inferir(imagen, nombre="foto.jpg")
    except InferenciaNoDisponible as e:
        return Response({"detalle": f"Inferencia no disponible: {e}"}, status=status.HTTP_502_BAD_GATEWAY)
    datos = {"lecturas": lecturas, "latencia_inferencia_ms": ms_inferencia}
    if lecturas:
        datos["consulta"] = cobro.consultar_placa(lecturas[0]["placa"])
    datos["latencia_ms"] = _ms(t0)
    return Response(datos)
