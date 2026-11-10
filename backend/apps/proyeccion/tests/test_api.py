import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.proyeccion import analisis
from apps.proyeccion.models import EstadoAnalisis, EstadoFicha, FichaNDC, FuenteFicha, OperacionProyectada, Proyeccion
from apps.proyeccion.tests.test_lectura import _csv
from apps.user.models import Organization
from apps.user.tests.fixtures import make_superadmin

User = get_user_model()

FILAS = (
    "Octubre - Proyección,,Uruguay,ANCAP,Plan de Inversiones 2026-2030,x,G,VP,Sur,Soberano,P,A,Préstamos,"
    "Corporativo,,,,1,,,,, 120 ,Préstamos,,",
    "Octubre - Proyección,,Uruguay,UTE,Parque Solar Fotovoltaico en Melo,x,G,VP,Sur,Soberano,P,A,PPI,Gobierno,"
    ",,,1,,,,, 75 ,Programas y Proyectos de Inversión,,",
    "Octubre - Nueva,,Brasil,Banco do Brasil S.A.,Línea de crédito,x,G,VP,Sur,Soberano,P,A,LC,IFD,,,,1,,,,, 50 ,"
    "Línea de Crédito,,",
    "Octubre - Nueva,,Brasil,Banco do Brasil S.A.,Línea de crédito,x,G,VP,Sur,Soberano,P,A,LC,IFD,,,,1,,,,, 50 ,"
    "Línea de Crédito,,",
    "Aprobada,,Chile,CMPC,Préstamo Corporativo,x,G,VP,Sur,No soberano,P,A,Préstamos,Corporativo,,,,1,,,,, 200 ,"
    "Préstamos,,",
)


def _archivo(*filas: str) -> SimpleUploadedFile:
    return SimpleUploadedFile("proyeccion.csv", _csv(*(filas or FILAS)), content_type="text/csv")


def _modelo_simulado(mensajes, esquema, *, modelo, max_tokens):
    """Responde como el modelo: fichas por país y un semáforo por operación."""
    import json

    pedido = mensajes[-1]["content"]
    if "conoce" in esquema["properties"]:
        if "Brasil" in pedido.split("</ndc>")[-1][:200]:
            return {"conoce": False, "version": "", "contenido": ""}, {"input_tokens": 10, "output_tokens": 5}
        return (
            {"conoce": True, "version": "NDC 2025", "contenido": "**Mitigación**\n- Renovables"},
            {"input_tokens": 10, "output_tokens": 5},
        )
    operaciones = json.loads(pedido.split("<operaciones>")[1].split("</operaciones>")[0])
    salida = []
    for op in operaciones:
        base = {
            "id": op["id"],
            "tipo_operacion": "Préstamo corporativo",
            "no_alineadas": "NINGUNA",
            "nota_no_alineadas": "",
            "riesgo_transicion": "NINGUNA",
            "riesgo_categoria": 0,
            "nota_riesgo": "",
            "oportunidad_ndc": "NINGUNA",
            "nota_oportunidad": "",
            "brecha_ndc": "NINGUNA",
            "nota_brecha": "",
            "usa_conocimiento_general": False,
        }
        if op["cliente"] == "ANCAP":
            base.update(
                no_alineadas="AMARILLO",
                nota_no_alineadas="Energética diversificada.",
                riesgo_transicion="ROJO",
                riesgo_categoria=2,
                nota_riesgo="Combustibles y cemento.",
                usa_conocimiento_general=True,
            )
        if op["cliente"] == "UTE":
            base.update(oportunidad_ndc="VERDE", nota_oportunidad="NDC prioriza renovables.")
        if op["cliente"].startswith("Banco do Brasil"):
            # Brasil no tiene ficha: esto no se le tiene que creer al modelo.
            base.update(brecha_ndc="ROJO", nota_brecha="Inventada.", nota_riesgo="Nota sin alerta.")
        salida.append(base)
    return {"operaciones": salida}, {"input_tokens": 100, "output_tokens": 50}


class _Base(APITestCase):
    def setUp(self):
        cache.clear()
        self.org_a = Organization.objects.create(name="A", slug="inst-a", enabled_features=["proyeccion"])
        self.org_b = Organization.objects.create(name="B", slug="inst-b", enabled_features=["proyeccion"])
        self.org_sin = Organization.objects.create(name="Sin", slug="inst-sin")
        self.miembro_a = User.objects.create_user(email="m@a.org", password="x", organization=self.org_a)
        self.miembro_b = User.objects.create_user(email="m@b.org", password="x", organization=self.org_b)
        self.miembro_sin = User.objects.create_user(email="m@sin.org", password="x", organization=self.org_sin)
        self.admin_a = User.objects.create_user(email="admin@a.org", password="x", organization=self.org_a, role="admin")
        self.publico = User.objects.create_user(email="p@gmail.com", password="x")
        self.superadmin = make_superadmin()
        despacho = mock.patch("apps.proyeccion.tasks.despachar_analisis")
        self.despachar_analisis = despacho.start()
        self.addCleanup(despacho.stop)
        despacho_ficha = mock.patch("apps.proyeccion.tasks.despachar_ficha")
        self.despachar_ficha = despacho_ficha.start()
        self.addCleanup(despacho_ficha.stop)

    def _como(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")

    def _cargar(self, periodo="2026-10", *filas, organization="inst-a", **extra):
        return self.client.post(
            f"/api/proyeccion/meses/?organization={organization}",
            {"periodo": periodo, "file": _archivo(*filas), **extra},
            format="multipart",
        )


class AccesoTestCase(_Base):
    def test_solo_entra_el_superadmin(self):
        # Ni los miembros, ni el administrador de la institución, ni quien no
        # tiene institución; y que la institución tenga «proyeccion» entre sus
        # secciones no abre nada.
        for user in (self.miembro_a, self.admin_a, self.miembro_sin, self.publico):
            self._como(user)
            self.assertEqual(self.client.get("/api/proyeccion/meses/", {"organization": "inst-a"}).status_code, 403)
            self.assertEqual(self._cargar().status_code, 403)
            self.assertEqual(self.client.get("/api/proyeccion/fichas-ndc/", {"organization": "inst-a"}).status_code, 403)
            self.assertEqual(
                self.client.post("/api/proyeccion/fichas-ndc/?organization=inst-a", {"pais": "Perú"}, format="json").status_code,
                403,
            )
        self.assertFalse(Proyeccion.objects.exists())
        self.assertFalse(FichaNDC.objects.exists())

    def test_lo_que_cargo_el_superadmin_no_lo_ve_la_institucion(self):
        self._como(self.superadmin)
        pk = self._cargar().json()["id"]
        ficha = FichaNDC.objects.create(organization=self.org_a, pais="Uruguay", contenido="x")
        for user in (self.miembro_a, self.admin_a, self.publico):
            self._como(user)
            self.assertEqual(self.client.get(f"/api/proyeccion/meses/{pk}/").status_code, 404)
            self.assertEqual(self.client.post(f"/api/proyeccion/meses/{pk}/analizar/", {}, format="json").status_code, 404)
            self.assertEqual(self.client.delete(f"/api/proyeccion/meses/{pk}/").status_code, 404)
            self.assertEqual(self.client.delete(f"/api/proyeccion/fichas-ndc/{ficha.pk}/").status_code, 404)
        self.assertTrue(Proyeccion.objects.filter(pk=pk).exists())
        self.assertTrue(FichaNDC.objects.filter(pk=ficha.pk).exists())
        self.despachar_analisis.assert_not_called()

    def test_el_superadmin_nombra_la_institucion_y_cada_una_tiene_lo_suyo(self):
        self._como(self.superadmin)
        self.assertEqual(self.client.get("/api/proyeccion/meses/").status_code, 400)
        self.assertEqual(self.client.get("/api/proyeccion/meses/", {"organization": "no-existe"}).status_code, 404)
        self.assertEqual(self._cargar(organization="inst-b").status_code, 201)
        FichaNDC.objects.create(organization=self.org_b, pais="Uruguay", contenido="de b")
        self.assertEqual(self.client.get("/api/proyeccion/meses/", {"organization": "inst-a"}).json(), [])
        self.assertEqual(self.client.get("/api/proyeccion/fichas-ndc/", {"organization": "inst-a"}).json(), [])
        self.assertEqual(len(self.client.get("/api/proyeccion/meses/", {"organization": "inst-b"}).json()), 1)
        # El mismo mes en otra institución es otra proyección, no un reemplazo.
        self.assertEqual(self._cargar(organization="inst-a").status_code, 201)
        self.assertEqual(Proyeccion.objects.count(), 2)


class CargaTestCase(_Base):
    def test_cargar_un_mes_guarda_las_operaciones(self):
        self._como(self.superadmin)
        respuesta = self._cargar()
        self.assertEqual(respuesta.status_code, 201, respuesta.content)
        cuerpo = respuesta.json()
        self.assertEqual(cuerpo["periodo"], "2026-10")
        self.assertEqual(cuerpo["total_operaciones"], 5)
        self.assertEqual(cuerpo["en_pipeline"], 4)
        self.assertEqual(cuerpo["estado"], "sin_analizar")
        codigos = [op["codigo"] for op in cuerpo["operaciones"]]
        self.assertEqual(codigos, ["OP-001", "OP-002", "OP-003", "OP-004", "OP-005"])
        self.assertEqual(cuerpo["operaciones"][0]["monto_usd_mm"], 120.0)
        # Sin mes anterior no se sabe qué es nuevo; las dos líneas iguales quedan marcadas.
        self.assertIsNone(cuerpo["operaciones"][0]["nueva"])
        self.assertEqual([op["posible_duplicado"] for op in cuerpo["operaciones"]], [False, False, True, True, False])

    def test_un_mes_ya_cargado_pide_confirmar_el_reemplazo(self):
        self._como(self.superadmin)
        primero = self._cargar().json()["id"]
        self.assertEqual(self._cargar().status_code, 409)
        reemplazo = self._cargar("2026-10", FILAS[0], reemplazar="true")
        self.assertEqual(reemplazo.status_code, 201)
        self.assertEqual(reemplazo.json()["id"], primero)
        self.assertEqual(OperacionProyectada.objects.filter(proyeccion_id=primero).count(), 1)

    def test_el_mes_siguiente_marca_las_nuevas_y_las_que_salieron(self):
        self._como(self.superadmin)
        self._cargar("2026-09", FILAS[0], FILAS[2])
        cuerpo = self._cargar("2026-10", FILAS[0], FILAS[1]).json()
        nuevas = {op["cliente"]: op["nueva"] for op in cuerpo["operaciones"]}
        self.assertEqual(nuevas, {"ANCAP": False, "UTE": True})
        self.assertEqual([op["cliente"] for op in cuerpo["salieron"]], ["Banco do Brasil S.A."])

    def test_un_archivo_que_no_es_la_proyeccion_da_400(self):
        self._como(self.superadmin)
        archivo = SimpleUploadedFile("otra.csv", b"a,b\n1,2\n")
        respuesta = self.client.post("/api/proyeccion/meses/?organization=inst-a", {"periodo": "2026-10", "file": archivo}, format="multipart")
        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(self._cargar("octubre").status_code, 400)


class AnalisisTestCase(_Base):
    def _analizar(self, cuerpo=None):
        self._como(self.superadmin)
        pk = self._cargar().json()["id"]
        respuesta = self.client.post(f"/api/proyeccion/meses/{pk}/analizar/", cuerpo or {}, format="json")
        self.assertEqual(respuesta.status_code, 202, respuesta.content)
        self.assertEqual(respuesta.json()["estado"], "en_cola")
        self.despachar_analisis.assert_called_once_with(pk)
        with (
            mock.patch.object(analisis, "_completar_json", side_effect=_modelo_simulado) as modelo,
            mock.patch.object(analisis, "_completar_texto", return_value=("Resumen del mes.", {})),
        ):
            analisis.analizar(pk)
        return pk, modelo

    def test_el_semaforo_respeta_la_regla_general(self):
        pk, modelo = self._analizar()
        cuerpo = self.client.get(f"/api/proyeccion/meses/{pk}/").json()
        self.assertEqual(cuerpo["estado"], "listo")
        self.assertEqual(cuerpo["resumen"], "Resumen del mes.")
        self.assertEqual(cuerpo["analizadas"], 4)
        ops = {op["codigo"]: op for op in cuerpo["operaciones"]}

        ancap = ops["OP-001"]
        self.assertEqual((ancap["no_alineadas"], ancap["riesgo_transicion"], ancap["riesgo_categoria"]), ("AMARILLO", "ROJO", 2))
        self.assertEqual(ancap["prioridad"], "ALTA")
        self.assertEqual(ancap["fuente_base"], "conocimiento_general")

        ute = ops["OP-002"]
        self.assertEqual((ute["oportunidad_ndc"], ute["fuente_ndc"], ute["prioridad"]), ("VERDE", "conocimiento_general", ""))

        # Brasil quedó sin ficha: la brecha que «encontró» el modelo no se guarda,
        # ni una nota que no acompaña a ninguna alerta.
        banco = ops["OP-003"]
        self.assertEqual((banco["brecha_ndc"], banco["nota_brecha"], banco["nota_riesgo"]), ("", "", ""))
        self.assertTrue(banco["analizada"])
        # La operación repetida no se manda dos veces, pero queda con el mismo resultado.
        self.assertEqual(ops["OP-004"]["analizada"], True)
        enviados = sum(
            llamada.args[0][-1]["content"].split("<operaciones>")[1].split("</operaciones>")[0].count('"id"')
            for llamada in modelo.call_args_list
            if "<operaciones>" in llamada.args[0][-1]["content"]
        )
        self.assertEqual(enviados, 3)
        # Lo que ya no es pipeline no se analiza.
        self.assertFalse(ops["OP-005"]["analizada"])

        fichas = {f.pais: f for f in FichaNDC.objects.filter(organization=self.org_a)}
        self.assertEqual(fichas["Uruguay"].estado, EstadoFicha.LISTA)
        self.assertEqual(fichas["Uruguay"].fuente, FuenteFicha.CONOCIMIENTO_GENERAL)
        self.assertEqual(fichas["Brasil"].estado, EstadoFicha.SIN_DATOS)

    def test_sin_ndc_no_se_arman_fichas_ni_alertas_de_ndc(self):
        pk, _ = self._analizar({"ndc": "ninguno"})
        self.assertFalse(FichaNDC.objects.exists())
        self.assertFalse(OperacionProyectada.objects.filter(proyeccion_id=pk).exclude(oportunidad_ndc="").exists())

    def test_se_puede_elegir_que_estados_analizar(self):
        pk, _ = self._analizar({"statuses": ["Aprobada"], "ndc": "ninguno"})
        analizadas = list(OperacionProyectada.objects.filter(proyeccion_id=pk, analizada=True).values_list("cliente", flat=True))
        self.assertEqual(analizadas, ["CMPC"])

    def test_un_analisis_en_curso_no_se_lanza_dos_veces(self):
        self._como(self.superadmin)
        pk = self._cargar().json()["id"]
        self.client.post(f"/api/proyeccion/meses/{pk}/analizar/", {}, format="json")
        self.assertEqual(self.client.post(f"/api/proyeccion/meses/{pk}/analizar/", {}, format="json").status_code, 400)
        # Y un mensaje repetido de la cola no lo corre de nuevo.
        Proyeccion.objects.filter(pk=pk).update(estado=EstadoAnalisis.LISTO)
        with mock.patch.object(analisis, "_analizar") as corrida:
            analisis.analizar(pk)
        corrida.assert_not_called()

    def test_si_el_modelo_falla_el_error_queda_a_la_vista(self):
        self._como(self.superadmin)
        pk = self._cargar().json()["id"]
        self.client.post(f"/api/proyeccion/meses/{pk}/analizar/", {"ndc": "ninguno"}, format="json")
        with (
            mock.patch.object(analisis, "_completar_json", side_effect=RuntimeError("sin servicio")),
            mock.patch.object(analisis, "_completar_texto", return_value=("Resumen.", {})),
        ):
            analisis.analizar(pk)
        cuerpo = self.client.get(f"/api/proyeccion/meses/{pk}/").json()
        self.assertEqual(cuerpo["estado"], "error")
        self.assertEqual(cuerpo["analizadas"], 0)
        self.assertIn("sin servicio", cuerpo["error"])
        # Y se puede volver a lanzar.
        self.assertEqual(self.client.post(f"/api/proyeccion/meses/{pk}/analizar/", {}, format="json").status_code, 202)

    def test_un_analisis_que_falla_no_borra_el_anterior(self):
        pk, _ = self._analizar({"ndc": "ninguno"})
        self.despachar_analisis.reset_mock()
        self.client.post(f"/api/proyeccion/meses/{pk}/analizar/", {"ndc": "ninguno"}, format="json")
        with mock.patch.object(analisis, "_completar_json", side_effect=RuntimeError("sin servicio")):
            analisis.analizar(pk)
        cuerpo = self.client.get(f"/api/proyeccion/meses/{pk}/").json()
        self.assertEqual((cuerpo["estado"], cuerpo["analizadas"]), ("error", 4))
        self.assertEqual(cuerpo["operaciones"][0]["riesgo_transicion"], "ROJO")

    def test_cambiar_el_alcance_no_deja_resultados_del_analisis_anterior(self):
        pk, _ = self._analizar({"ndc": "ninguno"})
        self.despachar_analisis.reset_mock()
        self.client.post(f"/api/proyeccion/meses/{pk}/analizar/", {"statuses": ["Aprobada"], "ndc": "ninguno"}, format="json")
        with (
            mock.patch.object(analisis, "_completar_json", side_effect=_modelo_simulado),
            mock.patch.object(analisis, "_completar_texto", return_value=("Resumen.", {})),
        ):
            analisis.analizar(pk)
        analizadas = list(OperacionProyectada.objects.filter(proyeccion_id=pk, analizada=True).values_list("cliente", flat=True))
        self.assertEqual(analizadas, ["CMPC"])
        self.assertFalse(OperacionProyectada.objects.filter(proyeccion_id=pk, riesgo_transicion="ROJO").exists())

    def test_un_lote_que_falla_deja_un_aviso_y_el_resto_se_guarda(self):
        self._como(self.superadmin)
        pk = self._cargar().json()["id"]
        self.client.post(f"/api/proyeccion/meses/{pk}/analizar/", {"ndc": "ninguno"}, format="json")

        def medio_modelo(mensajes, esquema, *, modelo, max_tokens):
            respuesta, uso = _modelo_simulado(mensajes, esquema, modelo=modelo, max_tokens=max_tokens)
            respuesta["operaciones"] = [op for op in respuesta["operaciones"] if op["id"] != "OP-002"]
            return respuesta, uso

        with (
            mock.patch.object(analisis, "_completar_json", side_effect=medio_modelo),
            mock.patch.object(analisis, "_completar_texto", return_value=("Resumen.", {})),
        ):
            analisis.analizar(pk)
        cuerpo = self.client.get(f"/api/proyeccion/meses/{pk}/").json()
        self.assertEqual((cuerpo["estado"], cuerpo["analizadas"]), ("listo", 3))
        self.assertIn("1 operaciones no se pudieron analizar", cuerpo["aviso"])


class FichasTestCase(_Base):
    def test_escribir_una_ficha_a_mano(self):
        self._como(self.superadmin)
        respuesta = self.client.post(
            "/api/proyeccion/fichas-ndc/?organization=inst-a", {"pais": "Perú", "contenido": "- Meta 2030", "version_ndc": "NDC 2025"}, format="json"
        )
        self.assertEqual(respuesta.status_code, 201, respuesta.content)
        self.assertEqual((respuesta.json()["estado"], respuesta.json()["fuente"]), ("lista", "manual"))
        self.despachar_ficha.assert_not_called()
        # El mismo país, escrito distinto, es la misma ficha.
        self.client.post("/api/proyeccion/fichas-ndc/?organization=inst-a", {"pais": "peru", "contenido": "- Otra"}, format="json")
        self.assertEqual(FichaNDC.objects.filter(organization=self.org_a).count(), 1)

    def test_armar_la_ficha_desde_la_ndc_adjunta(self):
        self._como(self.superadmin)
        ndc = SimpleUploadedFile("ndc-peru.txt", ("Contribución determinada a nivel nacional. " * 30).encode())
        respuesta = self.client.post("/api/proyeccion/fichas-ndc/?organization=inst-a", {"pais": "Perú", "file": ndc}, format="multipart")
        self.assertEqual(respuesta.status_code, 201, respuesta.content)
        self.assertEqual(respuesta.json()["estado"], "generando")
        ficha = FichaNDC.objects.get(organization=self.org_a)
        self.despachar_ficha.assert_called_once_with(ficha.pk)
        with mock.patch.object(analisis, "_completar_json", side_effect=_modelo_simulado) as modelo:
            analisis.generar_ficha(ficha.pk)
        self.assertIn("<ndc>", modelo.call_args.args[0][-1]["content"])
        ficha.refresh_from_db()
        self.assertEqual((ficha.estado, ficha.fuente, ficha.version_ndc), ("lista", "ndc", "NDC 2025"))

    def test_regional_no_tiene_ficha(self):
        self._como(self.superadmin)
        self.assertEqual(self.client.post("/api/proyeccion/fichas-ndc/?organization=inst-a", {"pais": "Regional"}, format="json").status_code, 400)


class SoloClaudeTestCase(_Base):
    """El módulo no corre con un modelo que no sea de Claude, esté como esté configurada la plataforma."""

    def test_el_modelo_es_de_claude_aunque_la_plataforma_este_en_otro_proveedor(self):
        with mock.patch.dict("os.environ", {"LLM_PROVIDER": "openai", "MODEL_COMPLETION": "gpt-4o-mini"}):
            os.environ.pop("PROYECCION_MODEL", None)
            os.environ.pop("LLM_MODEL_BALANCED", None)
            self.assertEqual(analisis.modelo_del_screening(), analisis.MODELO_POR_DEFECTO)
        with mock.patch.dict("os.environ", {"LLM_PROVIDER": "anthropic"}):
            os.environ.pop("PROYECCION_MODEL", None)
            os.environ.pop("LLM_MODEL_BALANCED", None)
            self.assertTrue(analisis.modelo_del_screening().startswith("claude"))

    def test_fijar_un_modelo_que_no_es_de_claude_hace_fallar_el_analisis_sin_llamar_a_nadie(self):
        self._como(self.superadmin)
        pk = self._cargar().json()["id"]
        self.client.post(f"/api/proyeccion/meses/{pk}/analizar/", {"ndc": "ninguno"}, format="json")
        with (
            mock.patch.dict("os.environ", {"PROYECCION_MODEL": "gpt-4o"}),
            mock.patch.object(analisis, "anthropic_structured_completion") as claude,
            mock.patch("apps.document.utils.client_openia.generate_chat_completion") as openai,
        ):
            analisis.analizar(pk)
        claude.assert_not_called()
        openai.assert_not_called()
        cuerpo = self.client.get(f"/api/proyeccion/meses/{pk}/").json()
        self.assertEqual(cuerpo["estado"], "error")
        self.assertIn("sólo usa Claude", cuerpo["error"])

    def test_las_llamadas_rechazan_un_modelo_que_no_es_de_claude(self):
        with mock.patch.object(analisis, "anthropic_structured_completion") as claude:
            with self.assertRaises(RuntimeError):
                analisis._completar_json([], {}, modelo="gpt-4o", max_tokens=10)
        claude.assert_not_called()
        with mock.patch.object(analisis, "anthropic_chat_completion") as claude:
            with self.assertRaises(RuntimeError):
                analisis._completar_texto([], modelo="gpt-4o-mini", max_tokens=10)
        claude.assert_not_called()
