"""API v1. Autenticación: JWT (app móvil) o sesión web + CSRF (modo caseta y consulta en el navegador)."""
import time

from django.conf import settings
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from . import cobro
from .inferencia import InferenciaNoDisponible, inferir
from .models import Caseta, LecturaPlaca

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
    description=("Lee la placa del frame, acumula lecturas de la caseta de los últimos 3 s y registra el paso "
                 "con la placa más repetida (>= 2 lecturas iguales o 1 con confianza > 0.85). "
                 "Anti-duplicado: la misma placa en la misma caseta no se cobra dos veces en 60 s.\n\n"
                 "`resultado`: `sin_placa` | `leyendo` (sin consenso aún) | `registrado` | `duplicado`."),
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

    try:
        lecturas, ms_inferencia = inferir(imagen)
    except InferenciaNoDisponible as e:
        return Response({"detalle": f"Inferencia no disponible: {e}", "latencia_ms": _ms(t0)},
                        status=status.HTTP_502_BAD_GATEWAY)

    base = {"lecturas": lecturas, "latencia_inferencia_ms": ms_inferencia}
    if not lecturas:
        return Response({**base, "resultado": "sin_placa", "latencia_ms": _ms(t0)})

    mejor = lecturas[0]
    LecturaPlaca.objects.create(caseta=caseta, placa=mejor["placa"], confianza=mejor["confianza"],
                                categoria=mejor["categoria"], ejes=mejor["ejes"])
    voto = cobro.votar(caseta)
    if voto is None:
        return Response({**base, "resultado": "leyendo", "placa_candidata": mejor["placa"], "latencia_ms": _ms(t0)})

    paso, duplicado, info = cobro.registrar_paso(
        caseta.id, voto["placa"], usuario=request.user, confianza=voto["confianza"],
        texto_leido=mejor["texto_leido"] if mejor["placa"] == voto["placa"] else voto["placa"],
        categoria=voto["categoria"], ejes=voto["ejes"],
    )
    if not duplicado:
        cobro.limpiar_lecturas_viejas()
    return Response({**base, "resultado": "duplicado" if duplicado else "registrado", "votos": voto["votos"],
                     "paso": cobro.paso_a_dict(paso, info), "latencia_ms": _ms(t0)})


@extend_schema(
    summary="Consulta el estado de una placa",
    description="Estados: `habilitada`, `saldo_insuficiente`, `inactiva`, `no_registrada` (+ sugerencias).",
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
        lecturas, ms_inferencia = inferir(imagen, nombre="foto.jpg")
    except InferenciaNoDisponible as e:
        return Response({"detalle": f"Inferencia no disponible: {e}"}, status=status.HTTP_502_BAD_GATEWAY)
    datos = {"lecturas": lecturas, "latencia_inferencia_ms": ms_inferencia}
    if lecturas:
        datos["consulta"] = cobro.consultar_placa(lecturas[0]["placa"])
    datos["latencia_ms"] = _ms(t0)
    return Response(datos)
