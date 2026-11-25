"""Tests del rediseño: panel (KPIs, gráfica, tiempo real), capturas, vehículos, alertas, consulta y estado."""
import hashlib
from pathlib import Path
import hmac
import json
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from . import panel, tiempo_real
from .models import Alerta, Caseta, Paso, Usuario, Vehiculo, caseta_principal
from .tests import entrar
from .tests_caseta import INFERIR, _jpeg, _lectura, pasar_vehiculo

PUSHER = {"PUSHER_APP_ID": "123", "PUSHER_KEY": "clave", "PUSHER_SECRET": "secreto", "PUSHER_CLUSTER": "us2"}


def _sincrono(funcion, *args):
    funcion(*args)


def _paso(placa, estado="cobrado", monto="0", alerta=None, hace=None, **extra):
    paso = Paso.objects.create(placa_leida=placa, placa_normalizada=placa, caseta=caseta_principal(), estado=estado,
                               monto=Decimal(monto), **extra)
    if hace is not None:
        Paso.objects.filter(pk=paso.pk).update(created_at=timezone.now() - hace)
        paso.refresh_from_db()
    if alerta:
        Alerta.objects.create(paso=paso, tipo=alerta)
    return paso


class MediaTemporal(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp(prefix="iap-test-")
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        ajustes = override_settings(MEDIA_ROOT=self.media, CLOUDINARY_URL="", PUSHER_APP_ID="")
        ajustes.enable()
        self.addCleanup(ajustes.disable)
        self.admin = Usuario.objects.get(email_google="lozogomol@gmail.com")
        self.oper = Usuario.objects.create(email_google="o@x.com", nombre="Olga", rol="operador")


class CasetaPrincipalTests(TestCase):
    def test_migracion_crea_peaje_iap_y_conserva_las_otras(self):
        self.assertEqual(caseta_principal().nombre, "Peaje IAP")
        self.assertEqual(Caseta.objects.count(), 4)

    def test_pagina_usa_la_caseta_principal(self):
        entrar(self.client, Usuario.objects.get(email_google="lozogomol@gmail.com"))
        r = self.client.get("/caseta")
        self.assertContains(r, "<h1>Punto de cobro</h1>", html=False)
        self.assertContains(r, f'<script id="caseta-id" type="application/json">{caseta_principal().id}</script>')
        for texto in ("Placa", "Tipo", "Ejes", "Pasajeros", "Esperando vehículo", "zoom"):
            self.assertContains(r, texto)
        self.assertNotContains(r, "Registrar paso")  # sin registro manual: solo Iniciar / Detener
        self.assertNotContains(r, "Senkata")


class PanelTests(MediaTemporal):
    def test_kpis_y_grafica(self):
        Paso.objects.all().delete()
        caseta = caseta_principal()
        Paso.objects.create(placa_leida="A", placa_normalizada="3876LBD", caseta=caseta, estado="cobrado",
                            monto=Decimal("10"))
        Paso.objects.create(placa_leida="B", placa_normalizada="1111AAA", caseta=caseta, estado="no_registrada")
        viejo = Paso.objects.create(placa_leida="C", placa_normalizada="2451KTP", caseta=caseta, estado="cobrado",
                                    monto=Decimal("5"))
        Paso.objects.filter(pk=viejo.pk).update(created_at=timezone.now() - timedelta(days=2))
        k = panel.kpis()
        self.assertEqual((k["pasos_hoy"], k["recaudacion_hoy"]), (2, "10.00"))
        self.assertEqual(k["alertas_pendientes"], Alerta.objects.filter(atendida=False).count())
        g = panel.grafica()
        self.assertEqual(len(g["valores"]), 7)
        self.assertEqual(g["valores"][-1], 2)
        self.assertEqual(sum(g["valores"]), 3)

    def test_pagina_y_json(self):
        entrar(self.client, self.oper)
        self.assertContains(self.client.get("/"), "Sin pasos registrados")
        _paso("3876LBD", monto="10")
        r = self.client.get("/")
        for texto in ("Pasos de hoy", "Recaudación de hoy", "Alertas sin atender", "Últimos pasos",
                      "chart.umd.min.js", "datos-panel"):
            self.assertContains(r, texto)
        d = self.client.get("/panel/datos").json()
        self.assertEqual(set(d), {"kpis", "grafica", "pasos"})
        self.assertLessEqual(len(d["pasos"]), 10)
        self.assertEqual(set(d["pasos"][0]),
                         {"id", "placa", "estado", "estado_texto", "categoria", "categoria_texto", "ejes", "pasajeros",
                          "monto", "hora", "fecha", "imagen_url", "ejes_texto", "tarifa", "cobrado", "resultado_texto"})

    def test_sin_pusher_no_carga_pusher_js(self):
        entrar(self.client, self.oper)
        self.assertNotContains(self.client.get("/"), "pusher.min.js")

    @override_settings(**PUSHER)
    def test_con_pusher_carga_pusher_js(self):
        entrar(self.client, self.oper)
        r = self.client.get("/")
        self.assertContains(r, "pusher.min.js")
        self.assertContains(r, "private-peaje")
        self.assertNotContains(r, "secreto")

    def test_json_requiere_sesion(self):
        self.assertEqual(self.client.get("/panel/datos").status_code, 302)


class FrameCapturaTiempoRealTests(MediaTemporal):
    def setUp(self):
        super().setUp()
        entrar(self.client, self.admin)
        self.caseta = caseta_principal()

    def _frame(self, placa):
        """Un vehículo completo: bloquea la placa, sale de cuadro y se cobra solo."""
        return pasar_vehiculo(self.client, self.caseta, [_lectura(placa, 0.95)])[1]

    def test_guarda_captura_local_y_la_sirve(self):
        d = self._frame("3876LBD").json()
        url = d["paso"]["imagen_url"]
        self.assertTrue(url.startswith("/capturas/"), url)
        self.assertEqual(Paso.objects.get(pk=d["paso"]["id"]).imagen_url, url)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b"".join(r.streaming_content), b"\xff\xd8\xff\xe0falso")
        self.assertEqual(self.client_class().get(url).status_code, 302)  # requiere sesión
        self.assertEqual(self.client.get("/capturas/../../config/settings.py").status_code, 404)
        # aparece en vehículos y en la consulta
        self.assertContains(self.client.get("/vehiculos?q=3876"), url)
        c = self.client.get("/api/v1/vehiculos/consulta", {"placa": "3876LBD"}).json()
        self.assertEqual(c["ultimo_paso"]["imagen_url"], url)

    @override_settings(**PUSHER)
    def test_emite_evento_con_alerta(self):
        with mock.patch.object(tiempo_real, "en_segundo_plano", _sincrono), \
                mock.patch("peaje.tiempo_real.requests.post") as post:
            self._frame("9999ZZZ")
        self.assertEqual(post.call_count, 1)
        cuerpo = json.loads(post.call_args.kwargs["data"])
        self.assertEqual((cuerpo["name"], cuerpo["channels"]), ("paso", ["private-peaje"]))
        datos = json.loads(cuerpo["data"])
        self.assertEqual(datos["paso"]["placa"], "9999ZZZ")
        self.assertEqual(datos["alerta"]["tipo"], "placa_no_registrada")
        self.assertEqual(set(datos), {"paso", "alerta", "kpis", "grafica"})
        # firma de la API REST de Pusher
        params = post.call_args.kwargs["params"]
        firma = params.pop("auth_signature")
        consulta = "&".join(f"{k}={params[k]}" for k in sorted(params))
        esperada = hmac.new(b"secreto", f"POST\n/apps/123/events\n{consulta}".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(firma, esperada)
        self.assertEqual(params["body_md5"], hashlib.md5(post.call_args.kwargs["data"].encode()).hexdigest())

    @override_settings(**PUSHER)
    def test_evento_y_panel_con_categoria_detectada(self):
        with mock.patch.object(tiempo_real, "en_segundo_plano", _sincrono),                 mock.patch("peaje.tiempo_real.requests.post") as post:
            pasar_vehiculo(self.client, self.caseta, [{**_lectura("2451KTP", 0.95), "categoria": "bus"}])
        paso = json.loads(json.loads(post.call_args.kwargs["data"])["data"])["paso"]
        self.assertEqual((paso["categoria"], paso["categoria_texto"]), ("bus", "Bus"))
        pasos = self.client.get("/panel/datos").json()["pasos"]
        self.assertEqual(pasos[0]["categoria_texto"], "Bus")
        r = self.client.get("/vehiculos?q=2451KTP")
        self.assertContains(r, '<td data-label="Categoría detectada">Bus</td>', html=False)

    @override_settings(**PUSHER)
    def test_duplicado_no_emite(self):
        self._frame("3876LBD")
        with mock.patch.object(tiempo_real, "en_segundo_plano") as fondo,                 mock.patch(INFERIR, return_value=([_lectura("3876LBD", 0.95)], 50, {})):
            r = self.client.post("/api/v1/pasos/frame", {"imagen": _jpeg(), "caseta_id": self.caseta.id})
        self.assertEqual(r.json()["accion"], "duplicado")
        fondo.assert_not_called()

    @override_settings(CLOUDINARY_URL="cloudinary://k123:s456@minube")
    def test_cloudinary(self):
        respuesta = mock.Mock(**{"json.return_value": {"secure_url": "https://res.cloudinary.com/x.jpg"}})
        with mock.patch.object(tiempo_real, "en_segundo_plano", _sincrono), \
                mock.patch("peaje.panel.connection.close"), \
                mock.patch("peaje.panel.requests.post", return_value=respuesta) as post:
            d = self._frame("3876LBD").json()
        self.assertEqual(Paso.objects.get(pk=d["paso"]["id"]).imagen_url, "https://res.cloudinary.com/x.jpg")
        self.assertIn("/v1_1/minube/image/upload", post.call_args.args[0])
        enviados = post.call_args.kwargs["data"]
        firmado = "&".join(f"{k}={enviados[k]}" for k in ("folder", "public_id", "timestamp")) + "s456"
        self.assertEqual(enviados["signature"], hashlib.sha1(firmado.encode()).hexdigest())


@override_settings(**PUSHER)
class PusherAuthTests(TestCase):
    def setUp(self):
        self.oper = Usuario.objects.create(email_google="o@x.com", nombre="Olga", rol="operador")

    def test_firma_canal_privado(self):
        entrar(self.client, self.oper)
        r = self.client.post("/pusher/auth", {"socket_id": "123.456", "channel_name": "private-peaje"})
        esperado = hmac.new(b"secreto", b"123.456:private-peaje", hashlib.sha256).hexdigest()
        self.assertEqual(r.json(), {"auth": f"clave:{esperado}"})

    def test_rechaza_otro_canal_o_sin_sesion(self):
        self.assertEqual(self.client.post("/pusher/auth", {"socket_id": "1.2", "channel_name": "private-peaje"})
                         .status_code, 302)
        entrar(self.client, self.oper)
        r = self.client.post("/pusher/auth", {"socket_id": "1.2", "channel_name": "private-otro"})
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/pusher/auth", {"socket_id": "x", "channel_name": "private-peaje"})
        self.assertEqual(r.status_code, 403)


class VehiculosTests(MediaTemporal):
    def test_registrar_normaliza_y_usa_valores_por_defecto(self):
        entrar(self.client, self.oper)
        r = self.client.post("/vehiculos", {"placa": "5555-abc", "saldo": "20"})
        self.assertRedirects(r, "/vehiculos", fetch_redirect_response=False)
        v = Vehiculo.objects.get(placa="5555ABC")
        self.assertEqual((v.propietario, v.saldo, v.categoria, v.ejes, v.activo), ("", Decimal("20"), "auto", 2, True))
        self.client.post("/vehiculos", {"placa": "666 XYZ"})  # saldo vacío = 0
        self.assertEqual(Vehiculo.objects.get(placa="666XYZ").saldo, Decimal("0"))

    def test_validaciones(self):
        entrar(self.client, self.oper)
        r = self.client.post("/vehiculos", {"placa": "AB12", "saldo": "0"})
        self.assertContains(r, "Formato no válido")
        self.assertContains(r, 'aria-invalid="true"')
        r = self.client.post("/vehiculos", {"placa": "3876 LBD", "saldo": "0"})
        self.assertContains(r, "ya está registrada")
        r = self.client.post("/vehiculos", {"placa": "7777XYZ", "saldo": "-5"})
        self.assertContains(r, "no puede ser negativo")
        self.assertFalse(Vehiculo.objects.filter(placa__in=["AB12", "7777XYZ"]).exists())

    def test_lista_busqueda_y_sin_pasos(self):
        entrar(self.client, self.oper)
        r = self.client.get("/vehiculos")
        self.assertContains(r, "Sin pasos")  # hay vehículos sin ningún paso
        self.assertContains(r, "Categoría detectada")
        self.assertNotContains(r, "Ejes")
        self.assertNotContains(r, "Propietario")
        self.assertNotContains(r, "Recargar")  # solo admin
        r = self.client.get("/vehiculos", {"q": "2451-k"})
        self.assertContains(r, "2451KTP")
        self.assertNotContains(r, "3876LBD")

    def test_recargar_y_activar_solo_admin(self):
        v = Vehiculo.objects.get(placa="1987FHK")
        entrar(self.client, self.oper)
        self.assertEqual(self.client.post(f"/vehiculos/{v.id}", {"accion": "recargar", "monto": "50"}).status_code, 403)
        entrar(self.client, self.admin)
        self.client.post(f"/vehiculos/{v.id}", {"accion": "recargar", "monto": "50"})
        self.client.post(f"/vehiculos/{v.id}", {"accion": "recargar", "monto": "-3"})
        self.client.post(f"/vehiculos/{v.id}", {"accion": "recargar", "monto": "abc"})
        self.assertEqual(Vehiculo.objects.get(pk=v.pk).saldo, Decimal("50"))
        self.client.post(f"/vehiculos/{v.id}", {"accion": "desactivar"})
        self.assertFalse(Vehiculo.objects.get(pk=v.pk).activo)
        self.assertContains(self.client.get("/vehiculos"), "Recargar")


class AlertasTests(MediaTemporal):
    def test_lista_filtro_y_atender(self):
        entrar(self.client, self.oper)
        r = self.client.get("/alertas")
        self.assertContains(r, "No hay alertas pendientes")
        _paso("6120PQR", estado="no_registrada", alerta="placa_no_registrada")
        r = self.client.get("/alertas")
        self.assertContains(r, "posible evasión")
        self.assertContains(r, "6120PQR")
        self.assertContains(r, "Marcar atendida")
        pendiente = Alerta.objects.filter(atendida=False).first()
        with mock.patch.object(tiempo_real, "notificar") as notificar:
            self.client.post(f"/alertas/{pendiente.id}/atender")
        self.assertTrue(Alerta.objects.get(pk=pendiente.pk).atendida)
        notificar.assert_called_once()
        self.assertEqual(notificar.call_args.args[0], "kpis")
        r = self.client.get("/alertas?filtro=atendidas")
        self.assertContains(r, pendiente.paso.placa_normalizada)
        self.assertNotContains(r, "Marcar atendida")


class ConsultaUltimoPasoTests(TestCase):
    def test_ultimo_paso(self):
        entrar(self.client, Usuario.objects.get(email_google="lozogomol@gmail.com"))
        self.assertIsNone(self.client.get("/api/v1/vehiculos/consulta", {"placa": "2451KTP"}).json()["ultimo_paso"])
        _paso("2451KTP", hace=timedelta(days=1))
        reciente = _paso("2451KTP", monto="5", imagen_url="/capturas/x.jpg")
        d = self.client.get("/api/v1/vehiculos/consulta", {"placa": "2451KTP"}).json()
        self.assertEqual(d["estado"], "habilitada")
        local = timezone.localtime(reciente.created_at)
        self.assertEqual((d["ultimo_paso"]["fecha"], d["ultimo_paso"]["hora"], d["ultimo_paso"]["imagen_url"]),
                         (local.strftime("%d/%m/%Y"), local.strftime("%H:%M:%S"), "/capturas/x.jpg"))
        self.assertEqual(d["vehiculo"]["propietario"], "")  # la clave JSON se mantiene
        _paso("6120PQR", estado="no_registrada")
        # placa no registrada que sí pasó (posible evasión)
        d = self.client.get("/api/v1/vehiculos/consulta", {"placa": "6120PQR"}).json()
        self.assertEqual((d["estado"], d["ultimo_paso"]["estado"]), ("no_registrada", "no_registrada"))


class EstadoPaginaTests(TestCase):
    @override_settings(INFERENCE_URL="http://inferencia")
    def test_tarjetas_con_modelos(self):
        salud = {"modelos": {"m1_placas": "cargado", "m2_vehiculos": "cargado", "m3_llantas": "pendiente",
                             "m4_pasajeros": "error"}}
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(salud).encode()
        with mock.patch("peaje.views.urllib.request.urlopen", return_value=resp):
            r = self.client.get("/estado")
        self.assertEqual(r.status_code, 200)
        for texto in ("Web", "Base de datos", "Inferencia", "Lectura de placas", "Tipo de vehículo", "Conteo de ejes",
                      "Conteo de pasajeros", "Activo", "Falta el archivo del modelo", "Error al cargar",
                      "Comprobar"):
            self.assertContains(r, texto)

    @override_settings(INFERENCE_URL="http://inferencia")
    def test_los_4_modelos_activos(self):
        salud = {"modelos": {"m1_placas": "cargado", "m2_vehiculos": "cargado", "m3_llantas": "cargado",
                             "m4_pasajeros": "cargado"}}
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(salud).encode()
        with mock.patch("peaje.views.urllib.request.urlopen", return_value=resp):
            r = self.client.get("/estado")
        for nombre in ("Lectura de placas", "Tipo de vehículo", "Conteo de ejes", "Conteo de pasajeros"):
            self.assertRegex(r.content.decode(), rf"<span>{nombre}</span><b class=\"texto-estado ok\">Activo</b>")

    @override_settings(INFERENCE_URL="http://inferencia")
    def test_inferencia_caida(self):
        with mock.patch("peaje.views.urllib.request.urlopen", side_effect=OSError("sin red")):
            r = self.client.get("/estado")
        self.assertEqual(r.status_code, 503)
        self.assertContains(r, "Sin respuesta", status_code=503)


class DisenoTests(TestCase):
    """Guardas del sistema de diseño "azul noche oscuro": sin emojis, esquinas rectas y paleta azul."""

    def _archivos(self):
        from pathlib import Path
        from django.conf import settings
        raiz = Path(settings.BASE_DIR)
        yield from (raiz / "peaje" / "templates").rglob("*.html")
        yield raiz / "static" / "css" / "iap.css"
        yield raiz / "peaje" / "templatetags" / "iconos.py"

    def _css(self):
        from pathlib import Path
        from django.conf import settings
        return (Path(settings.BASE_DIR) / "static" / "css" / "iap.css").read_text(encoding="utf-8")

    def test_sin_emojis_ni_rojo_anterior(self):
        import re
        emoji = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")
        rojo = re.compile(r"#(991b1b|7f1d1d|3b0a12|1a0508|0a0204|fca5a5|fecaca|fee2e2)\b", re.I)
        for archivo in self._archivos():
            texto = archivo.read_text(encoding="utf-8")
            for patron, que in ((emoji, "emoji"), (rojo, "rojo anterior")):
                m = patron.search(texto)
                self.assertIsNone(m, f"{que} en {archivo.name}: {m and m.group(0)!r}")

    def test_paleta_azul_noche_oscuro(self):
        css = self._css()
        for color in ("#030712", "#050b1e", "#0a1430", "#101e45", "#2b4fd8", "#1f3eb5", "#9db4ff",
                      "#ffffff", "#e2e8f0", "#16a34a", "#d97706", "#dc2626"):
            self.assertIn(color, css)
        self.assertIn("--ancho-max: 1280px", css)
        self.assertIn("--alto-control: 44px", css)
        self.assertIn('"Inter"', css)

    def test_esquinas_rectas_en_todo(self):
        import re
        css = self._css()
        self.assertIn("border-radius: 0 !important", css)
        for archivo in self._archivos():
            for valor in re.findall(r'border-radius\s*:\s*([^;}"]*)', archivo.read_text(encoding="utf-8"), re.I):
                self.assertRegex(valor.strip(), r"^0(px)?\s*(!important)?$", f"esquina redondeada en {archivo.name}")
        self.assertIn("cornerRadius: 0", (Path(__file__).parent / "templates" / "peaje" / "panel.html").read_text(encoding="utf-8"))

    def test_galaxia_animada_solo_en_barra_login_y_kpi(self):
        from django.conf import settings
        css = self._css()
        for animacion in ("@keyframes nebulosa", "@keyframes titilar", "prefers-reduced-motion"):
            self.assertIn(animacion, css)
        permitidos = {"base.html", "login.html", "panel.html"}
        for archivo in (Path(settings.BASE_DIR) / "peaje" / "templates").rglob("*.html"):
            if "galaxia" in archivo.read_text(encoding="utf-8"):
                self.assertIn(archivo.name, permitidos)

    def test_titulos_de_secciones(self):
        entrar(self.client, Usuario.objects.get(email_google="lozogomol@gmail.com"))
        for ruta, titulo in (("/", "Panel en tiempo real"), ("/caseta", "Punto de cobro"),
                             ("/consulta", "Consulta de placas"), ("/vehiculos", "Vehículos registrados"),
                             ("/pasos", "Historial de pasos"), ("/alertas", "Alertas"), ("/chat", "Asistente IA"),
                             ("/usuarios", "Usuarios y accesos")):
            r = self.client.get(ruta)
            self.assertContains(r, f"<h1>{titulo}</h1>", html=False)
            self.assertLessEqual(r.content.decode().count('class="subtitulo"'), 1, ruta)
        r = self.client.get("/")
        self.assertContains(r, 'id="usuario-btn"')
        self.assertContains(r, "Administrador")

    def test_confirmacion_y_tablas_apilables(self):
        entrar(self.client, Usuario.objects.get(email_google="lozogomol@gmail.com"))
        r = self.client.get("/vehiculos")
        self.assertContains(r, "data-confirmar=")
        self.assertContains(r, 'data-label="Placa"')
        self.assertContains(r, "css/iap.css")
        self.assertContains(r, 'aria-label="Abrir menú"')


class SeccionesFlujoTests(TestCase):
    """Panel por tipo, consulta con punto de cobro y CRUD de vehículos."""

    def setUp(self):
        self.admin = Usuario.objects.get(email_google="lozogomol@gmail.com")
        entrar(self.client, self.admin)

    def test_panel_vehiculos_por_tipo(self):
        Paso.objects.all().delete()
        for placa, cat in (("3876LBD", "auto"), ("1520GHR", "auto"), ("4471LCH", "bus"), ("6120PQR", "")):
            _paso(placa, categoria_detectada=cat)
        tipos = {f["texto"]: f["n"] for f in panel.kpis()["por_tipo"]}
        self.assertEqual((tipos["Auto"], tipos["Bus"], tipos["Moto"], tipos["Sin tipo"]), (2, 1, 0, 1))
        self.assertContains(self.client.get("/"), "Vehículos por tipo")

    def test_consulta_muestra_punto_de_cobro(self):
        _paso("3876LBD", categoria_detectada="auto", ejes_detectados=2, monto="4")
        u = self.client.get("/api/v1/vehiculos/consulta", {"placa": "3876LBD"}).json()["ultimo_paso"]
        self.assertEqual((u["caseta"], u["categoria_texto"], u["ejes"], u["monto"]), ("Peaje IAP", "Auto", 2, "4.00"))

    def test_eliminar_vehiculo_conserva_historial(self):
        v = Vehiculo.objects.get(placa="3876LBD")
        _paso("3876LBD")
        oper = Usuario.objects.create(email_google="o2@x.com", nombre="Olga", rol="operador")
        otro = self.client_class()
        entrar(otro, oper)
        self.assertEqual(otro.post(f"/vehiculos/{v.id}", {"accion": "eliminar"}).status_code, 403)
        self.assertContains(self.client.get("/vehiculos"), 'value="eliminar"')
        self.client.post(f"/vehiculos/{v.id}", {"accion": "eliminar"})
        self.assertFalse(Vehiculo.objects.filter(placa="3876LBD").exists())
        self.assertTrue(Paso.objects.filter(placa_normalizada="3876LBD").exists())
