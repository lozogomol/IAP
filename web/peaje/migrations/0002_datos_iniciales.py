"""Datos iniciales del peaje. Idempotente: se puede correr varias veces (Render ejecuta migrate en cada deploy)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from django.db import migrations

BOLIVIA = timezone(timedelta(hours=-4))

USUARIOS = [
    ("doc.alvaro.pinto.ma@unifranz.edu.bo", "Ing. Alvaro Pinto", "admin"),
    ("lozogomol@gmail.com", "Oliver Gonzales", "admin"),
]

CASETAS = [
    ("Caseta Senkata – El Alto", "Carretera La Paz–Oruro, Senkata, El Alto"),
    ("Caseta Achica Arriba – La Paz-Oruro", "Ruta 1, Achica Arriba, provincia Aroma, La Paz"),
    ("Caseta Copacabana", "Carretera El Alto–Copacabana, Tiquina, La Paz"),
]

TARIFAS = [
    ("moto", 2, 5), ("auto", 2, 10), ("minibus", 2, 15), ("bus", 2, 25), ("bus", 3, 35),
    ("camion", 2, 20), ("camion", 3, 30), ("camion", 4, 40), ("camion", 5, 50), ("camion", 6, 60),
]

# placa, propietario (ficticio), categoria, ejes, saldo, activo
VEHICULOS = [
    ("2451KTP", "Juan Carlos Mamani Quispe", "moto", 2, "120.00", True),
    ("3876LBD", "María Fernanda Rojas Choque", "auto", 2, "250.00", True),
    ("1520GHR", "Luis Alberto Condori Flores", "auto", 2, "80.00", True),
    ("4012MNS", "Ana Lucía Gutiérrez Vargas", "auto", 2, "150.00", True),
    ("987PLB", "Pedro Huanca Limachi", "auto", 2, "60.00", True),
    ("5630KRT", "Transportes Illimani S.R.L.", "minibus", 2, "300.00", True),
    ("2798JFN", "Rosa Apaza Ticona", "minibus", 2, "95.00", True),
    ("4471LCH", "Trans Copacabana S.A.", "bus", 2, "500.00", True),
    ("3305HDW", "Flota Bolívar Ltda.", "bus", 3, "420.00", True),
    ("1098GTY", "Carlos Eduardo Patzi Nina", "camion", 2, "200.00", True),
    ("2864KPL", "Transportes Andinos S.R.L.", "camion", 3, "350.00", True),
    ("4720MDX", "Freddy Callisaya Poma", "camion", 4, "400.00", True),
    ("3517LRF", "Logística Altiplano S.A.", "camion", 5, "600.00", True),
    ("5102NBT", "Carga Pesada Oruro Ltda.", "camion", 6, "750.00", True),
    ("654TRB", "Jorge Alanoca Cruz", "moto", 2, "40.00", False),      # inactivo
    ("1987FHK", "Silvia Mendoza Aruquipa", "auto", 2, "0.00", True),   # sin saldo
    ("4389MCW", "Mario Quisbert Laura", "minibus", 2, "3.50", True),   # saldo bajo
    ("2236JKV", "Transportes El Alto S.R.L.", "camion", 3, "0.00", True),  # sin saldo
    ("3042KMZ", "Patricia Chura Mamani", "auto", 2, "75.00", False),  # inactivo
    ("5511NHP", "Flota Oruro Express", "bus", 3, "380.00", True),
]

# placa leída por OCR, confianza, caseta (índice), estado, pasajeros, fecha (Bolivia), alerta (tipo, atendida)
PASOS = [
    ("2451-KTP", 0.97, 0, "cobrado", 1, datetime(2026, 11, 14, 7, 42, tzinfo=BOLIVIA), None),
    ("3876 LBD", 0.95, 0, "cobrado", 3, datetime(2026, 11, 14, 8, 15, tzinfo=BOLIVIA), None),
    ("5630KRT", 0.93, 1, "cobrado", 14, datetime(2026, 11, 15, 6, 58, tzinfo=BOLIVIA), None),
    ("4471-LCH", 0.96, 2, "cobrado", 38, datetime(2026, 11, 15, 10, 30, tzinfo=BOLIVIA), None),
    ("1098GTY", 0.91, 1, "cobrado", 2, datetime(2026, 11, 16, 13, 5, tzinfo=BOLIVIA), None),
    ("2864 KPL", 0.89, 1, "cobrado", 1, datetime(2026, 11, 17, 16, 20, tzinfo=BOLIVIA), None),
    ("1520GHR", 0.94, 2, "cobrado", 4, datetime(2026, 11, 18, 9, 47, tzinfo=BOLIVIA), None),
    ("1987-FHK", 0.92, 0, "saldo_insuficiente", 2, datetime(2026, 11, 19, 18, 12, tzinfo=BOLIVIA),
     ("saldo_insuficiente", True)),
    # placas NO registradas
    ("6120 PQR", 0.90, 0, "no_registrada", 1, datetime(2026, 11, 20, 11, 3, tzinfo=BOLIVIA),
     ("placa_no_registrada", False)),
    ("715-XKD", 0.86, 1, "no_registrada", 22, datetime(2026, 11, 20, 15, 36, tzinfo=BOLIVIA),
     ("placa_no_registrada", False)),
]

# categoría/ejes detectados para las placas no registradas
NO_REGISTRADAS = {"6120PQR": ("auto", 2), "715XKD": ("minibus", 2)}


def normalizar(texto):
    return "".join(c for c in texto.upper() if c.isalnum())


def cargar(apps, schema_editor):
    Usuario = apps.get_model("peaje", "Usuario")
    Caseta = apps.get_model("peaje", "Caseta")
    Tarifa = apps.get_model("peaje", "Tarifa")
    Vehiculo = apps.get_model("peaje", "Vehiculo")
    Paso = apps.get_model("peaje", "Paso")
    Alerta = apps.get_model("peaje", "Alerta")

    usuarios = {}
    for email, nombre, rol in USUARIOS:
        usuarios[email], _ = Usuario.objects.update_or_create(
            email_google=email, defaults={"nombre": nombre, "rol": rol, "activo": True})

    casetas = []
    for nombre, ubicacion in CASETAS:
        caseta, _ = Caseta.objects.update_or_create(nombre=nombre, defaults={"ubicacion": ubicacion})
        casetas.append(caseta)

    tarifas = {}
    for categoria, ejes, monto in TARIFAS:
        tarifas[(categoria, ejes)], _ = Tarifa.objects.update_or_create(
            categoria=categoria, ejes=ejes, defaults={"monto": Decimal(monto)})

    vehiculos = {}
    for placa, propietario, categoria, ejes, saldo, activo in VEHICULOS:
        # get_or_create: no pisa saldos que hayan cambiado en producción
        vehiculos[placa], _ = Vehiculo.objects.get_or_create(
            placa=placa, defaults={"propietario": propietario, "categoria": categoria, "ejes": ejes,
                                   "saldo": Decimal(saldo), "activo": activo})

    operador = usuarios["lozogomol@gmail.com"]
    for leida, confianza, i_caseta, estado, pasajeros, fecha, alerta in PASOS:
        placa = normalizar(leida)
        if placa in vehiculos:
            v = vehiculos[placa]
            categoria, ejes = v.categoria, v.ejes
        else:
            categoria, ejes = NO_REGISTRADAS[placa]
        monto = tarifas[(categoria, ejes)].monto if estado == "cobrado" else Decimal("0")

        paso = Paso.objects.filter(placa_normalizada=placa, created_at=fecha).first()
        if paso is None:
            paso = Paso.objects.create(
                placa_leida=leida, placa_normalizada=placa, confianza_ocr=confianza,
                categoria_detectada=categoria, ejes_detectados=ejes, monto=monto,
                caseta=casetas[i_caseta], usuario=operador, estado=estado, pasajeros=pasajeros)
            Paso.objects.filter(pk=paso.pk).update(created_at=fecha)  # auto_now_add ignora el valor al crear

        if alerta:
            tipo, atendida = alerta
            a, creada = Alerta.objects.get_or_create(paso=paso, tipo=tipo, defaults={"atendida": atendida})
            if creada:
                Alerta.objects.filter(pk=a.pk).update(created_at=fecha)


class Migration(migrations.Migration):
    dependencies = [("peaje", "0001_initial")]

    operations = [migrations.RunPython(cargar, migrations.RunPython.noop)]
