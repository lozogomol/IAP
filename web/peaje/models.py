"""Modelos de la base de datos del sistema de peaje IAP."""
from django.db import models

CATEGORIAS = [
    ("moto", "Moto"),
    ("auto", "Auto"),
    ("minibus", "Minibús"),
    ("camion", "Camión"),
    ("bus", "Bus"),
]


class Usuario(models.Model):
    """Lista blanca de cuentas Google autorizadas. Sin contraseñas: el login es solo con Google."""

    ROLES = [("admin", "Administrador"), ("operador", "Operador")]

    email_google = models.EmailField(unique=True)
    nombre = models.CharField(max_length=120)
    rol = models.CharField(max_length=10, choices=ROLES, default="operador")
    activo = models.BooleanField(default=True)
    # Solicitud de acceso hecha desde /acceso-denegado: queda activo=False hasta que un admin la apruebe.
    pendiente = models.BooleanField(default=False)
    solicitado_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "usuarios"
        ordering = ["nombre"]

    def __str__(self):
        return f"{self.nombre} <{self.email_google}>"


class Vehiculo(models.Model):
    placa = models.CharField(max_length=10, unique=True)
    propietario = models.CharField(max_length=120)
    categoria = models.CharField(max_length=10, choices=CATEGORIAS)
    ejes = models.PositiveSmallIntegerField(default=2)
    saldo = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = "vehiculos"
        ordering = ["placa"]

    def __str__(self):
        return f"{self.placa} ({self.get_categoria_display()}, {self.ejes} ejes)"


class Caseta(models.Model):
    nombre = models.CharField(max_length=120)
    ubicacion = models.CharField(max_length=200)

    class Meta:
        db_table = "casetas"
        ordering = ["nombre"]

    def __str__(self):
        return self.nombre


class Tarifa(models.Model):
    categoria = models.CharField(max_length=10, choices=CATEGORIAS)
    ejes = models.PositiveSmallIntegerField()
    monto = models.DecimalField(max_digits=8, decimal_places=2)

    class Meta:
        db_table = "tarifas"
        ordering = ["categoria", "ejes"]
        constraints = [
            models.UniqueConstraint(fields=["categoria", "ejes"], name="tarifa_unica_categoria_ejes"),
        ]

    def __str__(self):
        return f"{self.get_categoria_display()} / {self.ejes} ejes: Bs {self.monto}"


class Paso(models.Model):
    ESTADOS = [
        ("cobrado", "Cobrado"),
        ("saldo_insuficiente", "Saldo insuficiente"),
        ("no_registrada", "Placa no registrada"),
        ("error_lectura", "Error de lectura"),
    ]

    placa_leida = models.CharField(max_length=20)
    placa_normalizada = models.CharField(max_length=10, db_index=True)
    confianza_ocr = models.FloatField(null=True, blank=True)
    categoria_detectada = models.CharField(max_length=10, choices=CATEGORIAS, blank=True)
    ejes_detectados = models.PositiveSmallIntegerField(null=True, blank=True)
    monto = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    caseta = models.ForeignKey(Caseta, on_delete=models.PROTECT, related_name="pasos")
    usuario = models.ForeignKey(Usuario, on_delete=models.SET_NULL, null=True, blank=True, related_name="pasos")
    imagen_url = models.URLField(max_length=500, null=True, blank=True)
    estado = models.CharField(max_length=20, choices=ESTADOS)
    pasajeros = models.PositiveSmallIntegerField(null=True, blank=True)  # bonus: conteo de pasajeros
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "pasos"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.placa_normalizada} · {self.get_estado_display()} · {self.created_at:%Y-%m-%d %H:%M}"


class Alerta(models.Model):
    TIPOS = [
        ("placa_no_registrada", "Placa no registrada"),
        ("saldo_insuficiente", "Saldo insuficiente"),
    ]

    paso = models.ForeignKey(Paso, on_delete=models.CASCADE, related_name="alertas")
    tipo = models.CharField(max_length=20, choices=TIPOS)
    atendida = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "alertas"
        ordering = ["atendida", "-created_at"]

    def __str__(self):
        return f"{self.get_tipo_display()} · {self.paso.placa_normalizada}"


class LecturaPlaca(models.Model):
    """Lectura de placa de un frame (modo caseta). Se acumulan unos segundos para votar antes de registrar el paso."""

    caseta = models.ForeignKey(Caseta, on_delete=models.CASCADE, related_name="lecturas")
    placa = models.CharField(max_length=10)
    confianza = models.FloatField(null=True, blank=True)  # 0..1
    categoria = models.CharField(max_length=10, choices=CATEGORIAS, blank=True)
    ejes = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "lecturas_placa"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.placa} · {self.caseta_id} · {self.created_at:%H:%M:%S}"
