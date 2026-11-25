"""Borra SOLO los pasos (y lo que depende de ellos) y el estado temporal del punto de cobro.

Borra: pasos, sus evidencias de imagen, las alertas de esos pasos (ligadas por clave foránea: no pueden quedar
sin su paso) y el estado temporal del punto de cobro (vehículo en curso y lecturas de placa en votación).
NO toca usuarios, vehículos (ni sus saldos), tarifas ni casetas.

    python manage.py limpiar_pasos               # solo muestra los conteos (no borra nada)
    python manage.py limpiar_pasos --confirmar   # borra y muestra los conteos antes y después
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum

from peaje.models import (Alerta, Caseta, EvidenciaImagen, LecturaPlaca, Paso, PasoEnCurso, Tarifa, Usuario,
                          Vehiculo)

# En el orden en que se borran (primero lo que depende de los pasos).
A_BORRAR = [
    ("alertas", Alerta),
    ("evidencias", EvidenciaImagen),
    ("pasos en curso", PasoEnCurso),
    ("lecturas de placa", LecturaPlaca),
    ("pasos", Paso),
]
A_CONSERVAR = [("usuarios", Usuario), ("vehiculos", Vehiculo), ("tarifas", Tarifa), ("casetas", Caseta)]
SALDOS = "saldo total (Bs)"


def conteos():
    datos = {nombre: modelo.objects.count() for nombre, modelo in A_BORRAR + A_CONSERVAR}
    datos[SALDOS] = Vehiculo.objects.aggregate(t=Sum("saldo"))["t"] or 0
    return datos


class Command(BaseCommand):
    help = ("Borra solo los pasos, sus evidencias, sus alertas y el estado temporal del punto de cobro. "
            "Conserva usuarios, vehículos (y saldos), tarifas y casetas. Exige --confirmar.")

    def add_arguments(self, parser):
        parser.add_argument("--confirmar", action="store_true", help="Borra de verdad (sin esto solo muestra conteos).")

    def _mostrar(self, titulo, datos):
        self.stdout.write(titulo)
        conservar = dict(A_CONSERVAR)
        for nombre, _ in A_BORRAR + A_CONSERVAR:
            marca = "conserva" if nombre in conservar else "borra"
            self.stdout.write(f"  {nombre:<18} {datos[nombre]:>8}   ({marca})")
        self.stdout.write(f"  {SALDOS:<18} {datos[SALDOS]:>8}   (conserva)")

    def handle(self, *args, **opciones):
        antes = conteos()
        self._mostrar("Antes:", antes)
        if not opciones["confirmar"]:
            raise CommandError("No se borró nada. Ejecuta de nuevo con --confirmar para borrar.")
        with transaction.atomic():
            for _, modelo in A_BORRAR:
                modelo.objects.all().delete()
            despues = conteos()
            cambiados = [n for n in list(dict(A_CONSERVAR)) + [SALDOS] if despues[n] != antes[n]]
            if cambiados:  # nunca debería pasar: se deshace todo
                raise CommandError(f"Cambió algo que debía conservarse ({', '.join(cambiados)}); no se borró nada.")
        self._mostrar("Después:", despues)
        self.stdout.write(self.style.SUCCESS(
            f"Listo: {antes['pasos']} pasos, {antes['evidencias']} evidencias y {antes['alertas']} alertas borrados "
            "(usuarios, vehículos y saldos, tarifas y casetas intactos)."))
