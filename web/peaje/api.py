"""API v1. Autenticación: JWT (app móvil) o sesión web + CSRF (modo caseta y consulta en el navegador)."""
import base64
import logging
import time

from django.conf import settings
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from . import cobro, panel
from .inferencia import InferenciaNoDisponible, inferir
from .models import Caseta, LecturaPlaca

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
        "Lee la placa y el tipo de vehículo del frame y devuelve SIEMPRE el resultado de ese frame, aunque no "
        "registre paso. Votación: registra con >= 2 lecturas iguales en 4 s o 1 lectura con confianza >= 0.80. "
        "Anti-duplicado: la misma placa en la misma caseta no se cobra dos veces en 30 s. "
        "Respuesta: `lectura` {placa, confianza, categoria, confianza_categoria, ejes, llantas_detectadas, "
        "pasajeros, latencia_ms, latencia_modelos_ms, caja_placa, caja_vehiculo, cajas_placas, cajas_llantas} (cajas [x1, y1, x2, y2] en píxeles del frame enviado), "
        "`lectura.categoria` es la clase (modelo.names) del vehículo que contiene la placa leída, o del más grande y "
        "centrado si no hay placa. `accion`: `registrado` | `esperando_confirmacion` | `duplicado` | `sin_placa` | `error`, "
        "`paso` (si existe), `mensaje` (texto corto en español), `votos`/`votos_necesarios` y "
        "`segundos_desde_ultimo_paso` (si es duplicado). Si la inferencia falla responde 502 con `accion: error`."
    ),
    request=inline_serializer("FrameEntrada", fields={
        "imagen": serializers.ImageField(), "caseta_id": serializers.IntegerField()}),
    responses={200: _respuesta, 400: _respuesta, 502: _respuesta},
)
@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
def paso_frame(request):
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
    if mejor is None:
        return responder("sin_placa", "Buscando placa…", lectura)

    LecturaPlaca.objects.create(caseta=caseta, placa=mejor["placa"], confianza=mejor["confianza"],
                                categoria=mejor["categoria"], ejes=mejor["ejes"], pasajeros=vehiculo.get("pasajeros"))
    voto = cobro.votar(caseta)
    if voto is None:
        previo = cobro.paso_reciente(caseta, mejor["placa"])
        if previo:
            return _duplicado(responder, previo, lectura)
        votos = cobro.votos_de(caseta, mejor["placa"])
        return responder("esperando_confirmacion",
                         f"Leyendo {mejor['placa']} (confirmando {votos}/{cobro.MIN_VOTOS})", lectura,
                         votos=votos, votos_necesarios=cobro.MIN_VOTOS)

    paso, duplicado, info = cobro.registrar_paso(
        caseta.id, voto["placa"], usuario=request.user, confianza=voto["confianza"],
        texto_leido=mejor["texto_leido"] if mejor["placa"] == voto["placa"] else voto["placa"],
        categoria=voto["categoria"], ejes=voto["ejes"], pasajeros=voto["pasajeros"],
    )
    if duplicado:
        return _duplicado(responder, paso, lectura)
    # Evidencia: el frame con las personas cubiertas por su máscara (privacidad); sin personas, el frame tal cual.
    panel.publicar_paso(paso, vehiculo.get("evidencia") or imagen)  # captura + evento en tiempo real
    cobro.limpiar_lecturas_viejas()
    evidencia = vehiculo.get("evidencia")
    return responder("registrado", f"Registrado: {paso.get_estado_display().lower()}", lectura,
                     paso=cobro.paso_a_dict(paso, info), votos=voto["votos"],
                     captura=("data:image/jpeg;base64," + base64.b64encode(evidencia).decode()) if evidencia else None)


def _duplicado(responder, paso, lectura):
    segundos = max(0, round((timezone.now() - paso.created_at).total_seconds()))
    return responder("duplicado", f"Ya registrado hace {segundos} s", lectura,
                     paso=cobro.paso_a_dict(paso), segundos_desde_ultimo_paso=segundos)


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
