from django.contrib import admin

from apps.proyeccion.models import FichaNDC, OperacionProyectada, Proyeccion


@admin.register(Proyeccion)
class ProyeccionAdmin(admin.ModelAdmin):
    list_display = ("organization", "periodo", "estado", "archivo_nombre", "analizado_en")
    list_filter = ("organization", "estado")
    readonly_fields = ("creado_en", "actualizado_en", "latido", "analizado_en", "uso")


@admin.register(OperacionProyectada)
class OperacionProyectadaAdmin(admin.ModelAdmin):
    list_display = ("codigo", "proyeccion", "pais", "cliente", "no_alineadas", "riesgo_transicion")
    list_filter = ("proyeccion__organization", "no_alineadas", "riesgo_transicion")
    search_fields = ("cliente", "descripcion", "pais")


@admin.register(FichaNDC)
class FichaNDCAdmin(admin.ModelAdmin):
    list_display = ("organization", "pais", "estado", "fuente", "version_ndc", "actualizado_en")
    list_filter = ("organization", "estado", "fuente")
    exclude = ("texto_ndc",)
