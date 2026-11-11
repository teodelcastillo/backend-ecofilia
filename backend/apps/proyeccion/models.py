"""
Proyección mensual del pipeline (módulo de propuesta, aislado).

Una vez por mes la institución recibe el Excel de la proyección (la pestaña
«Proyección DPO - fecha»). De cada operación se toma lo poco que trae —país,
cliente, descripción, monto y producto— y un asistente hace un screening
preliminar con cuatro alertas tempranas:

1. actividades no alineadas;
2. riesgo de transición;
3. oportunidad de alineación con la NDC del país;
4. brecha de alineación con la NDC del país.

El módulo es una propuesta y vive aparte a propósito: tiene sus tablas, sus
rutas (``/api/proyeccion/``) y su sección en el portal, y no lee ni escribe
operaciones, documentos ni corridas. Lo único que comparte con el resto es la
institución (``Organization``), el usuario, el cliente del modelo de lenguaje
y —sólo para leer— las NDC de la biblioteca pública.

Quién lo ve: sólo el superadmin de Ecofilia (ver ``apps.proyeccion.acceso``).
Y sólo usa modelos de Claude (ver ``apps.proyeccion.analisis``).
"""
from django.conf import settings
from django.db import models


class EstadoAnalisis(models.TextChoices):
    SIN_ANALIZAR = "sin_analizar", "Sin analizar"
    EN_COLA = "en_cola", "En cola"
    ANALIZANDO = "analizando", "Analizando"
    LISTO = "listo", "Listo"
    ERROR = "error", "Error"


class AlcanceNDC(models.TextChoices):
    """Para qué países se aplican las alertas 3 y 4."""

    TODOS = "todos", "Todos los países"
    CON_FICHA = "con_ficha", "Sólo los países con ficha NDC"
    NINGUNO = "ninguno", "Sin alertas NDC"


class Nivel:
    """Valores del semáforo. Vacío es «sin indicios»: la regla general."""

    ROJO = "ROJO"
    AMARILLO = "AMARILLO"
    SIN_INFO = "S/I"
    VERDE = "VERDE"
    POSIBLE = "POSIBLE"

    RIESGO = (ROJO, AMARILLO, SIN_INFO)
    OPORTUNIDAD = (VERDE, POSIBLE)
    BRECHA = (ROJO, AMARILLO)


class Proyeccion(models.Model):
    """La proyección de un mes: una por institución y mes."""

    organization = models.ForeignKey(
        "user.Organization", on_delete=models.CASCADE, related_name="proyecciones"
    )
    # Siempre el día 1 del mes.
    periodo = models.DateField()
    archivo_nombre = models.CharField(max_length=255, blank=True)
    hoja = models.CharField(max_length=255, blank=True)
    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    estado = models.CharField(
        max_length=16, choices=EstadoAnalisis.choices, default=EstadoAnalisis.SIN_ANALIZAR
    )
    # Con qué alcance se pidió el último análisis: estados del Excel y países con NDC.
    alcance = models.JSONField(default=dict, blank=True)
    etapa = models.CharField(max_length=32, blank=True)
    progreso_hechas = models.PositiveIntegerField(default=0)
    progreso_total = models.PositiveIntegerField(default=0)
    # Última señal de vida del análisis: sin ella no se distingue uno que
    # trabaja de uno cuyo worker murió.
    latido = models.DateTimeField(null=True, blank=True)
    analizado_en = models.DateTimeField(null=True, blank=True)
    resumen = models.TextField(blank=True)
    aviso = models.TextField(blank=True)
    error = models.TextField(blank=True)
    modelo = models.CharField(max_length=64, blank=True)
    uso = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-periodo"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "periodo"], name="proyeccion_unica_por_mes"),
        ]

    def __str__(self):
        return f"{self.organization.slug} · {self.periodo:%Y-%m}"


class OperacionProyectada(models.Model):
    """Una fila del Excel, con el resultado del screening si se analizó."""

    proyeccion = models.ForeignKey(Proyeccion, on_delete=models.CASCADE, related_name="operaciones")
    orden = models.PositiveIntegerField()
    codigo = models.CharField(max_length=16)

    # Lo que trae el Excel.
    status = models.CharField(max_length=120, blank=True)
    pais = models.CharField(max_length=120, blank=True)
    cliente = models.CharField(max_length=500, blank=True)
    descripcion = models.TextField(blank=True)
    producto = models.CharField(max_length=255, blank=True)
    monto_usd_mm = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    fecha_cci_ido_texto = models.CharField(max_length=60, blank=True)
    fecha_cci_ido = models.DateField(null=True, blank=True)

    # Lo que se deduce al cargar. ``nueva`` es None si no hay mes anterior.
    nueva = models.BooleanField(null=True, blank=True)
    posible_duplicado = models.BooleanField(default=False)

    # El screening.
    analizada = models.BooleanField(default=False)
    tipo_operacion = models.CharField(max_length=80, blank=True)
    no_alineadas = models.CharField(max_length=12, blank=True)
    nota_no_alineadas = models.TextField(blank=True)
    riesgo_transicion = models.CharField(max_length=12, blank=True)
    riesgo_categoria = models.PositiveSmallIntegerField(null=True, blank=True)
    nota_riesgo = models.TextField(blank=True)
    oportunidad_ndc = models.CharField(max_length=12, blank=True)
    nota_oportunidad = models.TextField(blank=True)
    brecha_ndc = models.CharField(max_length=12, blank=True)
    nota_brecha = models.TextField(blank=True)
    # De dónde sale lo dicho: ``excel`` o ``conocimiento_general`` para las
    # alertas 1 y 2; ``ndc`` o ``conocimiento_general`` para las 3 y 4.
    fuente_base = models.CharField(max_length=24, blank=True)
    fuente_ndc = models.CharField(max_length=24, blank=True)

    class Meta:
        ordering = ["orden"]
        indexes = [models.Index(fields=["proyeccion", "orden"])]

    def __str__(self):
        return f"{self.codigo} · {self.cliente[:60]}"


class FuenteFicha(models.TextChoices):
    NDC = "ndc", "NDC adjunta"
    CONOCIMIENTO_GENERAL = "conocimiento_general", "Conocimiento general"
    MANUAL = "manual", "Escrita a mano"


class EstadoFicha(models.TextChoices):
    GENERANDO = "generando", "Generando"
    LISTA = "lista", "Lista"
    # El modelo no conoce la NDC con suficiente seguridad: no se completa.
    SIN_DATOS = "sin_datos", "Sin datos suficientes"
    ERROR = "error", "Error"


class FichaNDC(models.Model):
    """La referencia breve de la NDC de un país, que usan las alertas 3 y 4.

    Se arma con la NDC vigente del país en la biblioteca pública
    (``apps.proyeccion.biblioteca``), una vez, y se reutiliza los meses
    siguientes; se rehace sola cuando la biblioteca tiene una NDC más nueva.
    """

    organization = models.ForeignKey(
        "user.Organization", on_delete=models.CASCADE, related_name="fichas_ndc"
    )
    pais = models.CharField(max_length=120)
    estado = models.CharField(max_length=16, choices=EstadoFicha.choices, default=EstadoFicha.LISTA)
    fuente = models.CharField(
        max_length=24, choices=FuenteFicha.choices, default=FuenteFicha.CONOCIMIENTO_GENERAL
    )
    # La NDC de la biblioteca pública con la que se armó la ficha. Si la
    # biblioteca pasa a tener una más nueva de ese país, la ficha se rehace.
    documento = models.ForeignKey(
        "document.Document", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    documento_nombre = models.CharField(max_length=255, blank=True)
    documento_anio = models.IntegerField(null=True, blank=True)
    version_ndc = models.CharField(max_length=255, blank=True)
    contenido = models.TextField(blank=True)
    # El texto de la NDC adjunta, para volver a armar la ficha sin subirla otra vez.
    texto_ndc = models.TextField(blank=True)
    archivo_nombre = models.CharField(max_length=255, blank=True)
    error = models.TextField(blank=True)
    actualizado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["pais"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "pais"], name="ficha_ndc_unica_por_pais"),
        ]

    def __str__(self):
        return f"{self.organization.slug} · {self.pais}"

    @property
    def utilizable(self) -> bool:
        return self.estado == EstadoFicha.LISTA and bool(self.contenido.strip())
