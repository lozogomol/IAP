"""Evidencias de los pasos en la BASE DE DATOS (nada en el disco del servidor: el de Render se borra en cada deploy).

La imagen que llega ya trae a las personas cubiertas por su máscara (la evidencia que arma la inferencia con M4; si
no había personas, el frame). Aquí se reduce a JPEG de lado mayor <= 960 px y calidad 75 y se guarda en
EvidenciaImagen; el paso apunta a /pasos/<id>/imagen.
"""
import io
import logging

from django.urls import reverse
from PIL import Image, ImageOps, UnidentifiedImageError

from .models import EvidenciaImagen, Paso

log = logging.getLogger(__name__)

LADO_MAX = 960
CALIDAD = 75


def reducir_jpeg(datos, lado_max=LADO_MAX, calidad=CALIDAD):
    """(bytes JPEG, ancho, alto) con el lado mayor <= lado_max, o None si los datos no son una imagen."""
    try:
        img = Image.open(io.BytesIO(datos))
        img = ImageOps.exif_transpose(img)  # orientación correcta si viene de una foto con EXIF
        img = img.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as e:
        log.warning("Evidencia descartada: no es una imagen válida (%s)", e)
        return None
    img.thumbnail((lado_max, lado_max), Image.LANCZOS)
    salida = io.BytesIO()
    img.save(salida, "JPEG", quality=calidad, optimize=True)
    return salida.getvalue(), img.width, img.height


def url_imagen(paso_id):
    return reverse("paso_imagen", args=[paso_id])


def guardar_evidencia(paso, imagen):
    """Guarda la evidencia del paso en la base de datos y le asigna su URL. Devuelve True si se guardó."""
    reducida = reducir_jpeg(imagen) if imagen else None
    if reducida is None:
        return False
    datos, ancho, alto = reducida
    EvidenciaImagen.objects.update_or_create(paso=paso, defaults={"datos": datos, "tipo": "image/jpeg",
                                                                   "ancho": ancho, "alto": alto})
    paso.imagen_url = url_imagen(paso.pk)
    Paso.objects.filter(pk=paso.pk).update(imagen_url=paso.imagen_url)
    return True
