import io
import zipfile
from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from apps.proyeccion.lectura import ArchivoInvalido, clave_operacion, en_pipeline, leer_proyeccion

ENCABEZADO = (
    "Status,PA,País,Cliente,Descripción,Código,Gerencia,VP,Región,Riesgo,Producto MPO,Tipo,"
    "Producto Agrupado,Tipo de Cliente,Fecha CCI - IDO,Fecha CCI-Aprob,Fecha Resolución,Mes Número,"
    'Etapa, Monto Por Aprobar , Monto Directo , Monto indirecto ,"Monto 2026\n(USD MM)",Producto, USD MM FV ,PAIS'
)


def _csv(*filas: str) -> bytes:
    titulo = ",,  DPO (Pipeline - SOFÍA)" + "," * 20
    return "\n".join([titulo, ENCABEZADO, *filas]).encode("cp1252")


class LeerCSVTestCase(SimpleTestCase):
    def test_lee_las_columnas_por_su_encabezado(self):
        lectura = leer_proyeccion(
            "proyeccion.csv",
            _csv(
                'Octubre - Proyección,,Perú,Compañía Eléctrica,"Parque eólico, fase II",x,G,VP,Norte,Soberano,'
                'P,Aprobación,Préstamos,Gobierno,4/21/2026,,,4,2. Originación," $300,000,000.00 ",,, 300 ,Préstamos, 10 ,otro',
                "Aprobada,,Brasil,BNDES,Línea de crédito,x,G,VP,Sur,Soberano,P,Aprobación,LC,IFD,Por definir,,,1,,,,,"
                '"1,250.5",Línea de Crédito,,',
                ",,,,,,,,,,,,,,,,,,,,,,,,,",
            ),
        )
        self.assertEqual(len(lectura.filas), 2)
        primera, segunda = lectura.filas
        self.assertEqual(primera.pais, "Perú")
        self.assertEqual(primera.cliente, "Compañía Eléctrica")
        self.assertEqual(primera.descripcion, "Parque eólico, fase II")
        self.assertEqual(primera.producto, "Préstamos")
        self.assertEqual(primera.monto_usd_mm, Decimal("300.00"))
        self.assertEqual(primera.fecha_cci_ido, date(2026, 4, 21))
        self.assertEqual(primera.status, "Octubre - Proyección")
        self.assertEqual(segunda.monto_usd_mm, Decimal("1250.50"))
        self.assertIsNone(segunda.fecha_cci_ido)
        self.assertEqual(segunda.fecha_cci_ido_texto, "Por definir")

    def test_un_archivo_sin_la_tabla_se_rechaza_con_un_mensaje_claro(self):
        with self.assertRaises(ArchivoInvalido) as error:
            leer_proyeccion("otra.csv", b"a,b,c\n1,2,3\n")
        self.assertIn("País", str(error.exception))

    def test_otro_formato_se_rechaza(self):
        with self.assertRaises(ArchivoInvalido):
            leer_proyeccion("proyeccion.pdf", b"%PDF")
        with self.assertRaises(ArchivoInvalido):
            leer_proyeccion("proyeccion.xlsx", b"esto no es un zip")


def _xlsx(hojas: dict[str, list[list]]) -> bytes:
    """Un .xlsx mínimo: cadenas compartidas para el texto y números en crudo."""
    cadenas: list[str] = []

    def celda(columna: int, fila: int, valor) -> str:
        letra = ""
        n = columna + 1
        while n:
            n, resto = divmod(n - 1, 26)
            letra = chr(65 + resto) + letra
        if isinstance(valor, (int, float)):
            return f'<c r="{letra}{fila}"><v>{valor}</v></c>'
        if valor not in cadenas:
            cadenas.append(valor)
        return f'<c r="{letra}{fila}" t="s"><v>{cadenas.index(valor)}</v></c>'

    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    archivos = {}
    for indice, (_, filas) in enumerate(hojas.items(), start=1):
        cuerpo = "".join(
            f'<row r="{f}">' + "".join(celda(c, f, v) for c, v in enumerate(fila) if v != "") + "</row>"
            for f, fila in enumerate(filas, start=1)
        )
        archivos[f"xl/worksheets/sheet{indice}.xml"] = f"<worksheet {ns}><sheetData>{cuerpo}</sheetData></worksheet>"
    archivos["xl/workbook.xml"] = (
        f'<workbook {ns} xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
        + "".join(
            f'<sheet name="{nombre}" sheetId="{i}" r:id="rId{i}"/>' for i, nombre in enumerate(hojas, start=1)
        )
        + "</sheets></workbook>"
    )
    archivos["xl/_rels/workbook.xml.rels"] = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{i}" Type="x" Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(hojas) + 1)
        )
        + "</Relationships>"
    )
    archivos["xl/sharedStrings.xml"] = (
        f"<sst {ns}>" + "".join(f"<si><t>{c}</t></si>" for c in cadenas) + "</sst>"
    )
    salida = io.BytesIO()
    with zipfile.ZipFile(salida, "w") as zf:
        for nombre, contenido in archivos.items():
            zf.writestr(nombre, contenido)
    return salida.getvalue()


class LeerXLSXTestCase(SimpleTestCase):
    def test_toma_la_pestana_de_la_proyeccion_y_convierte_fechas_y_montos(self):
        encabezado = ["Status", "PA", "País", "Cliente", "Descripción"] + [""] * 9 + ["Fecha CCI - IDO"]
        encabezado += [""] * 7 + ["Monto 2026 (USD MM)", "Producto"]
        fila = ["Octubre - Nueva", "", "Uruguay", "UTE", "Parque Solar en Melo"] + [""] * 9 + [46133]
        fila += [""] * 7 + [75, "Programas y Proyectos de Inversión"]
        contenido = _xlsx(
            {
                "Resumen": [["País", "Cliente", "Descripción"], ["Chile", "No es esta hoja", "x"]],
                "Proyección DPO Oct-12": [["", "", "DPO (Pipeline)"], encabezado, fila],
            }
        )
        lectura = leer_proyeccion("GACBP.xlsx", contenido)
        self.assertEqual(lectura.hoja, "Proyección DPO Oct-12")
        self.assertEqual(len(lectura.filas), 1)
        op = lectura.filas[0]
        self.assertEqual((op.pais, op.cliente, op.descripcion), ("Uruguay", "UTE", "Parque Solar en Melo"))
        self.assertEqual(op.monto_usd_mm, Decimal("75.00"))
        self.assertEqual(op.producto, "Programas y Proyectos de Inversión")
        self.assertEqual(op.fecha_cci_ido, date(2026, 4, 21))


class ReglasTestCase(SimpleTestCase):
    def test_que_es_pipeline(self):
        self.assertTrue(en_pipeline("Octubre - Proyección"))
        self.assertTrue(en_pipeline('Octubre - "Nueva; antes desembolso"'))
        self.assertFalse(en_pipeline("Aprobada"))
        self.assertFalse(en_pipeline("Desistida"))
        self.assertFalse(en_pipeline("desembolsada "))

    def test_la_clave_no_depende_de_acentos_ni_espacios(self):
        self.assertEqual(
            clave_operacion("Perú", "  Compañía  Eléctrica", "Línea"),
            clave_operacion("peru", "compania electrica", "linea"),
        )
