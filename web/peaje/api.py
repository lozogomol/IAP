"""API v1. Autenticación: JWT (app móvil) o sesión web + CSRF (modo caseta y consulta en el navegador)."""
import base64
import json
import logging
import time

from django.conf import settings
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from . import cobro, flujo, panel
from .inferencia import InferenciaNoDisponible, inferir
from .servicios import agente
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
        "otra placa distinta o 10 min sin verla. Placa no registrada o con saldo insuficiente: el paso queda "
        "`pendiente_pago` (con su alerta en tiempo real) y pasa a `cobrado` con forma de pago `efectivo` cuando el "
        "vehículo SALE del cuadro (2 frames seguidos y >= 3 s sin su placa ni ningún vehículo): `accion: salida`. "
        "Si se confirma otra placa antes de que salga del todo, primero se cierra el anterior (llega en `anterior`). "
        "Respuesta: `accion` `sin_placa` | `esperando_confirmacion` | `en_curso` | `registrado` | `fijo` | `salida` | "
        "`error`; `flujo` {etapa, placa, placa_bloqueada, tipo, tipo_texto, tipo_listo, ejes, ejes_listo, pasajeros, "
        "pasajeros_listo, segundos}; `lectura` (resultado de ESTE frame: placa, confianza, categoria, ejes, "
        "pasajeros, cajas [x1, y1, x2, y2] en píxeles del frame enviado, `formas` con el polígono del vehículo, "
        "keypoints y eje de cada llanta, siluetas de pasajeros y la caja de la placa, latencias); `paso` (si se registró o es el "
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
        # formas reales para dibujar sobre el video (polígono M2, keypoints M3, siluetas M4, caja de placa M1);
        # null si el servidor de inferencia aún no las manda (se dibujan las cajas)
        "formas": vehiculo.get("formas"),
    }
    r = flujo.procesar_frame(caseta, lecturas, vehiculo, imagen, usuario=request.user)
    accion, estado_flujo = r["accion"], r["flujo"]
    if r.get("resuelto"):
        # El vehículo salió (o llegó otro antes de que saliera del todo): su paso pendiente se cobró en efectivo.
        panel.publicar_pago(r["resuelto"])  # panel, alertas y punto de cobro en vivo
        if accion != "salida":
            responder = _con_anterior(responder, r["resuelto"])
    if accion == "sin_placa":
        return responder("sin_placa", "Buscando placa…", lectura, flujo=estado_flujo)
    if accion == "esperando_confirmacion":
        return responder("esperando_confirmacion",
                         f"Leyendo {mejor['placa']} (confirmando {r['votos']}/{cobro.MIN_VOTOS})", lectura,
                         flujo=estado_flujo, votos=r["votos"], votos_necesarios=cobro.MIN_VOTOS)
    if accion == "salida":
        # El vehículo salió del cuadro: la tarjeta muestra "Cobrado - salió" y vuelve a la espera.
        return responder("salida", f"Placa {r['paso'].placa_normalizada} · Cobrado - salió", lectura,
                         flujo=estado_flujo, paso=cobro.paso_a_dict(r["paso"]))
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


@extend_schema(
    summary="Cierra el punto de cobro (botón Detener)",
    description=("El vehículo en pantalla se da por salido: si su paso estaba `pendiente_pago`, pasa a `cobrado` "
                 "en `efectivo` y su alerta queda atendida con la nota \"Cobrado al cerrar el punto de cobro\" "
                 "(se emite por Pusher). Responde `{resuelto: paso | null}`."),
    request=inline_serializer("CerrarEntrada", fields={"caseta_id": serializers.IntegerField()}),
    responses={200: _respuesta, 400: _respuesta},
)
@api_view(["POST"])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def caseta_cerrar(request):
    cid = str(request.data.get("caseta_id", ""))
    caseta = Caseta.objects.filter(pk=int(cid)).first() if cid.isdigit() else None
    if caseta is None:
        return Response({"detalle": "caseta_id inválido."}, status=status.HTTP_400_BAD_REQUEST)
    paso = flujo.cerrar_punto(caseta)
    if paso:
        panel.publicar_pago(paso)
    return Response({"resuelto": cobro.paso_a_dict(paso) if paso else None})


def _con_anterior(responder, paso):
    def envuelto(*args, **kwargs):
        return responder(*args, anterior=cobro.paso_a_dict(paso), **kwargs)
    return envuelto


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


_turnos = inline_serializer("TurnoChat", fields={
    "rol": serializers.ChoiceField(choices=["usuario", "asistente"]), "texto": serializers.CharField()},
    many=True, required=False)


@extend_schema(
    summary="Asistente: pregunta en lenguaje natural sobre una placa (texto y/o foto)",
    description=(
        "Agente conversacional con Gemini (function calling; si el modelo principal está saturado, el de respaldo) "
        "y, si no hay clave o la API falla, un agente por reglas con las mismas herramientas: `consultar_placa`, "
        "`historial_placa`, `pasos_por_fecha`, `resumen_dia` y `leer_placa_imagen` (inferencia M1 sobre la imagen "
        "adjunta). Las respuestas salen solo de la base de datos del peaje; nunca se inventan datos. Solo atiende "
        "consultas del peaje: otros temas reciben un mensaje de límite (`estado: limite`). "
        "Entrada: JSON `{mensaje, historial?}` o multipart con `mensaje`, `historial` (JSON) e `imagen`. "
        "`historial`: lista de `{rol: usuario|asistente, texto}` (se usan los últimos 10). "
        "Respuesta: `respuesta` (texto), `estado` (`registrada_con_paso` | `registrada_sin_paso` | "
        "`identificada_no_registrada` | `no_existe` | `pasos_por_fecha` | `resumen_dia` | `sin_lectura` | `sin_imagen` | "
        "`inferencia_no_disponible` | `limite` | `ayuda`), `placa`, `sugerencias` (¿Quisiste decir…?), `datos` (lo "
        "que devolvió la herramienta, con `ultimo_paso` e `imagen_url` si existe), `motor` (`gemini` | `reglas`), "
        "`modelo` (el modelo de Gemini que respondió) y `motivo` (por qué respondió el agente por reglas)."
    ),
    request={
        "application/json": inline_serializer("ChatEntrada", fields={
            "mensaje": serializers.CharField(required=False), "historial": _turnos}),
        "multipart/form-data": inline_serializer("ChatEntradaFoto", fields={
            "mensaje": serializers.CharField(required=False), "historial": serializers.CharField(required=False),
            "imagen": serializers.ImageField(required=False)}),
    },
    responses={200: inline_serializer("ChatRespuesta", fields={
        "respuesta": serializers.CharField(), "estado": serializers.CharField(),
        "placa": serializers.CharField(allow_null=True), "sugerencias": serializers.ListField(child=serializers.CharField()),
        "datos": serializers.DictField(allow_null=True), "motor": serializers.CharField(),
        "modelo": serializers.CharField(allow_null=True), "motivo": serializers.CharField(allow_null=True)}),
        400: _respuesta},
)
@api_view(["POST"])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def agente_chat(request):
    mensaje = str(request.data.get("mensaje") or "").strip()[:1000]
    historial = request.data.get("historial") or []
    if isinstance(historial, str):
        try:
            historial = json.loads(historial)
        except ValueError:
            historial = []
    if not isinstance(historial, list):
        historial = []
    historial = [t for t in historial if isinstance(t, dict)]
    imagen = None
    if request.FILES.get("imagen") is not None:
        imagen, error = _imagen(request)
        if error:
            return error
    if not mensaje and imagen is None:
        return Response({"detalle": "Escribe un mensaje o adjunta una imagen."}, status=status.HTTP_400_BAD_REQUEST)
    return Response(agente.responder(mensaje, historial, imagen))
