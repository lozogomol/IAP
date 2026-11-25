"""Modelos de la base de datos del sistema de peaje IAP."""
from django.db import models
from django.utils import timezone

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
    propietario = models.CharField(max_length=120, blank=True, default="")  # esquema mínimo; no se muestra
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
    """Un vehículo que pasó por un punto de cobro. Caseta real: si pasó, pagó.

    - Registrada con saldo suficiente: "cobrado" con forma_pago "saldo" (se descuenta el saldo).
    - No registrada o con saldo insuficiente: "pendiente_pago" (forma_pago vacía) + su alerta en tiempo real; pasa a
      "cobrado" con forma_pago "efectivo" cuando el vehículo sale del cuadro (flujo.py), al cerrar el punto de cobro
      o cuando el operador atiende la alerta (ver cobro.cobrar_en_efectivo). No se toca el saldo.
    """

    ESTADOS = [
        ("cobrado", "Cobrado"),
        ("pendiente_pago", "Pendiente de pago"),
        ("error_lectura", "Error de lectura"),
    ]
    FORMAS_PAGO = [("saldo", "Saldo"), ("efectivo", "Efectivo")]

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
    forma_pago = models.CharField(max_length=10, choices=FORMAS_PAGO, blank=True, default="")  # vacía = pendiente
    pasajeros = models.PositiveSmallIntegerField(null=True, blank=True)  # M4: conteo de pasajeros (null = sin dato)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "pasos"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.placa_normalizada} · {self.get_estado_display()} · {self.created_at:%Y-%m-%d %H:%M}"

    # `monto` es SIEMPRE la tarifa calculada (tipo + ejes), se haya cobrado o no; `estado` dice si se cobró.
    @property
    def cobrado(self):
        return self.estado == "cobrado"

    @property
    def pendiente(self):
        return self.estado == "pendiente_pago"

    @property
    def registrada(self):
        """La placa estaba registrada (y activa) al pasar: no tiene alerta de placa no registrada."""
        return not any(a.tipo == "placa_no_registrada" for a in self.alertas.all())

    @property
    def motivo_pendiente(self):
        """Tipo de alerta del paso (placa_no_registrada | saldo_insuficiente), o "" si no tiene."""
        alerta = next(iter(self.alertas.all()), None)  # usa prefetch_related("alertas") si lo hay
        return alerta.tipo if alerta else ""

    @property
    def tarifa(self):
        """Tarifa del paso en Bs, o None si no se pudo calcular (sin tipo de vehículo)."""
        return self.monto if self.monto or self.categoria_detectada else None

    @property
    def monto_cobrado(self):
        return self.monto if self.cobrado else 0

    @property
    def resultado_texto(self):
        """"Cobrado con saldo" | "Cobrado en efectivo" | "Pendiente de pago (no registrada)" | ..."""
        if self.cobrado:
            return {"saldo": "Cobrado con saldo", "efectivo": "Cobrado en efectivo"}.get(self.forma_pago, "Cobrado")
        if self.pendiente:
            motivo = MOTIVO_PENDIENTE.get(self.motivo_pendiente)
            return f"Pendiente de pago ({motivo})" if motivo else "Pendiente de pago"
        return "No cobrado: error de lectura"

    @property
    def ejes_tarifa(self):
        return max(2, self.ejes_detectados or 2)

    @property
    def ejes_texto(self):
        """Ejes con los que se calculó la tarifa: sin dato de M3 se cobran 2 y se muestra "2 (estimado)"."""
        return "2 (estimado)" if self.ejes_detectados is None else str(self.ejes_tarifa)


MOTIVO_PENDIENTE = {"placa_no_registrada": "no registrada", "saldo_insuficiente": "saldo insuficiente"}


class EvidenciaImagen(models.Model):
    """Captura de evidencia del paso, guardada EN LA BASE DE DATOS (el disco de Render es efímero).

    JPEG reducido (lado mayor <= 960 px, calidad 75) del mejor frame de la placa, con las personas ya cubiertas por
    su máscara (M4). Se sirve en /pasos/<id>/imagen solo con sesión iniciada.
    """

    paso = models.OneToOneField(Paso, on_delete=models.CASCADE, primary_key=True, related_name="evidencia")
    datos = models.BinaryField()
    tipo = models.CharField(max_length=30, default="image/jpeg")
    ancho = models.PositiveSmallIntegerField()
    alto = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "evidencias_imagen"

    def __str__(self):
        return f"Evidencia del paso {self.paso_id} ({self.ancho}x{self.alto}, {len(self.datos)} bytes)"


class Alerta(models.Model):
    TIPOS = [
        ("placa_no_registrada", "Placa no registrada"),
        ("saldo_insuficiente", "Saldo insuficiente"),
    ]

    paso = models.ForeignKey(Paso, on_delete=models.CASCADE, related_name="alertas")
    tipo = models.CharField(max_length=20, choices=TIPOS)
    nota = models.CharField(max_length=120, blank=True, default="")  # cómo se resolvió
    atendida_at = models.DateTimeField(null=True, blank=True)
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
    confianza_categoria = models.FloatField(null=True, blank=True)  # 0..1, confianza de M2 en `categoria`
    ejes = models.PositiveSmallIntegerField(null=True, blank=True)
    pasajeros = models.PositiveSmallIntegerField(null=True, blank=True)  # M4: personas dentro del vehículo
    created_at = models.DateTimeField(default=timezone.now, db_index=True)  # hora del frame

    class Meta:
        db_table = "lecturas_placa"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.placa} · {self.caseta_id} · {self.created_at:%H:%M:%S}"


class PasoEnCurso(models.Model):
    """Vehículo que está pasando ahora por una caseta (máquina de estados del punto de cobro, una fila por caseta).

    La placa se bloquea al confirmarse por votación y no cambia hasta registrar el paso. Tipo (M2) y ejes (M3) se
    observan 1.5 s desde su primera detección (votos_tipo = {clase: suma de confianzas}, votos_ejes = {ejes: frames})
    y recién ahí se bloquean; los pasajeros (M4) se acumulan por frame ({valor: cantidad}). Al registrar se limpia la captura y el paso
    queda en `paso` como resultado fijo hasta que se confirme una placa distinta (ver flujo.py).
    """

    caseta = models.OneToOneField(Caseta, on_delete=models.CASCADE, primary_key=True, related_name="en_curso")
    placa = models.CharField(max_length=10, blank=True)  # vacía = esperando placa
    texto_leido = models.CharField(max_length=20, blank=True)
    confianza = models.FloatField(null=True, blank=True)
    bloqueada_at = models.DateTimeField(null=True, blank=True)
    ultimo_visto_at = models.DateTimeField(null=True, blank=True)
    ultimo_frame_at = models.DateTimeField(null=True, blank=True)
    reiniciado_at = models.DateTimeField(null=True, blank=True)  # lecturas anteriores no votan por la placa
    votos_tipo = models.JSONField(default=dict, blank=True)
    tipo = models.CharField(max_length=10, choices=CATEGORIAS, blank=True)  # bloqueado al cerrar la observación
    tipo_desde = models.DateTimeField(null=True, blank=True)  # primera detección M2 del vehículo (inicio de la ventana)
    votos_ejes = models.JSONField(default=dict, blank=True)
    ejes = models.PositiveSmallIntegerField(null=True, blank=True)  # bloqueados al cerrar la observación
    ejes_desde = models.DateTimeField(null=True, blank=True)  # primera detección de ejes (M3)
    ultimo_eje = models.PositiveSmallIntegerField(null=True, blank=True)  # último conteo de ejes observado
    votos_pasajeros = models.JSONField(default=dict, blank=True)
    evidencia = models.BinaryField(null=True, blank=True)  # mejor frame de la placa (personas cubiertas)
    evidencia_confianza = models.FloatField(null=True, blank=True)
    # Resultado fijo en pantalla: el último paso registrado (placa en blanco = no hay vehículo en captura).
    paso = models.ForeignKey(Paso, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # Salida del vehículo del resultado fijo: frames seguidos sin su placa ni ningún vehículo, y cuándo salió.
    frames_sin_vehiculo = models.PositiveSmallIntegerField(default=0)
    salida_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "pasos_en_curso"

    def __str__(self):
        return f"{self.caseta_id} · {self.placa or 'esperando placa'}"


CASETA_PRINCIPAL = "Peaje IAP"


def caseta_principal():
    """La caseta que usa el modo caseta (creada por la migración 0004; se recrea si alguien la borra)."""
    caseta = Caseta.objects.filter(nombre=CASETA_PRINCIPAL).order_by("id").first()
    return caseta or Caseta.objects.create(nombre=CASETA_PRINCIPAL, ubicacion=CASETA_PRINCIPAL)
