"""Deja la app como nueva: borra los datos de operación y conserva la configuración.

Borra: pasos, evidencias de imagen, alertas, vehículos y el estado temporal del punto de cobro (pasos en curso y
lecturas de placa). NO borra usuarios, tarifas ni casetas. No toca código ni módulos.

    python manage.py limpiar_datos               # solo muestra los conteos (no borra nada)
    python manage.py limpiar_datos --confirmar   # borra y muestra los conteos antes y después
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from peaje.models import (Alerta, Caseta, EvidenciaImagen, LecturaPlaca, Paso, PasoEnCurso, Tarifa, Usuario,
                          Vehiculo)

# En el orden en que se borran (primero lo que depende de los pasos).
A_BORRAR = [
    ("alertas", Alerta),
    ("evidencias", EvidenciaImagen),
    ("pasos en curso", PasoEnCurso),
    ("lecturas de placa", LecturaPlaca),
    ("pasos", Paso),
    ("vehiculos", Vehiculo),
]
A_CONSERVAR = [("usuarios", Usuario), ("tarifas", Tarifa), ("casetas", Caseta)]


def conteos():
    return {nombre: modelo.objects.count() for nombre, modelo in A_BORRAR + A_CONSERVAR}


class Command(BaseCommand):
    help = ("Borra pasos, evidencias, alertas, vehículos y el estado temporal del punto de cobro. "
            "Conserva usuarios, tarifas y casetas. Exige --confirmar.")

    def add_arguments(self, parser):
        parser.add_argument("--confirmar", action="store_true", help="Borra de verdad (sin esto solo muestra conteos).")

    def _mostrar(self, titulo, datos):
        self.stdout.write(titulo)
        for nombre, _ in A_BORRAR + A_CONSERVAR:
            marca = "conserva" if nombre in dict(A_CONSERVAR) else "borra"
            self.stdout.write(f"  {nombre:<18} {datos[nombre]:>6}   ({marca})")

    def handle(self, *args, **opciones):
        antes = conteos()
        self._mostrar("Antes:", antes)
        if not opciones["confirmar"]:
            raise CommandError("No se borró nada. Ejecuta de nuevo con --confirmar para borrar.")
        with transaction.atomic():
            for _, modelo in A_BORRAR:
                modelo.objects.all().delete()
            despues = conteos()
            cambiados = [n for n, _ in A_CONSERVAR if despues[n] != antes[n]]
            if cambiados:  # nunca debería pasar: se deshace todo
                raise CommandError(f"Cambió algo que debía conservarse ({', '.join(cambiados)}); no se borró nada.")
        self._mostrar("Después:", despues)
        self.stdout.write(self.style.SUCCESS("Listo: la app quedó como nueva (usuarios, tarifas y casetas intactos)."))
