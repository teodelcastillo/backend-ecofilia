"""Leer el archivo de la proyección: Excel (.xlsx) o CSV.

Del archivo importan cinco columnas —país, cliente, descripción, monto y
producto— más el estado de la fila y la fecha de CCI/IDO, que sirven para
elegir qué se analiza y qué es próximo. El resto lo completan otras áreas y no
siempre está: se ignora.

Las columnas se ubican por el nombre del encabezado, no por la letra: la
planilla cambia de un mes a otro. La posición (C, D, E, W, X) queda sólo como
respaldo para monto y producto.

El .xlsx se lee con la biblioteca estándar (es un zip de XML) para no sumar
una dependencia al backend por un módulo de propuesta.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

MAX_BYTES = 15 * 1024 * 1024
_MAX_MEMBER_BYTES = 80 * 1024 * 1024
_MAX_FILAS = 5000
_MAX_COLUMNAS = 120
_FILAS_PARA_ENCABEZADO = 25

# Respaldo posicional: columnas W y X de la pestaña «Proyección DPO».
_COL_MONTO = 22
_COL_PRODUCTO = 23

# Estados de una fila que ya no es pipeline: se cargan, pero no se analizan
# salvo que se pida.
ESTADOS_CERRADOS = ("aprobada", "desembolsada", "desistida")


class ArchivoInvalido(ValueError):
    """El archivo no se puede leer como una proyección. El mensaje es para quien lo subió."""


@dataclass
class FilaProyeccion:
    status: str
    pais: str
    cliente: str
    descripcion: str
    producto: str
    monto_usd_mm: Decimal | None
    fecha_cci_ido_texto: str
    fecha_cci_ido: date | None


@dataclass
class LecturaProyeccion:
    hoja: str
    filas: list[FilaProyeccion]


def plegar(texto: str) -> str:
    """Sin acentos, en minúsculas y con los espacios colapsados: para comparar."""
    descompuesto = unicodedata.normalize("NFD", str(texto or ""))
    sin_acentos = "".join(ch for ch in descompuesto if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", sin_acentos).strip().lower()


def en_pipeline(status: str) -> bool:
    return not plegar(status).startswith(ESTADOS_CERRADOS)


def clave_operacion(pais: str, cliente: str, descripcion: str) -> str:
    """Lo que identifica a una operación de un mes a otro (el Excel no trae un id estable)."""
    return "|".join(plegar(parte) for parte in (pais, cliente, descripcion))


# ---------------------------------------------------------------------------
# Archivo → hojas de celdas
# ---------------------------------------------------------------------------

_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "p": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_M = "{%s}" % _NS["m"]


def _leer_miembro(archivo: zipfile.ZipFile, nombre: str) -> bytes:
    info = archivo.getinfo(nombre)
    if info.file_size > _MAX_MEMBER_BYTES:
        raise ArchivoInvalido("El Excel es demasiado grande para leerlo.")
    return archivo.read(info)


def _indice_de_columna(referencia: str) -> int:
    """«C5» → 2."""
    indice = 0
    for ch in referencia:
        if not ch.isalpha():
            break
        indice = indice * 26 + (ord(ch.upper()) - 64)
    return indice - 1


def _cadenas_compartidas(archivo: zipfile.ZipFile) -> list[str]:
    try:
        contenido = _leer_miembro(archivo, "xl/sharedStrings.xml")
    except KeyError:
        return []
    cadenas: list[str] = []
    for si in ElementTree.fromstring(contenido).findall("m:si", _NS):
        # Texto simple (<t>) o con formato (<r><t>); la guía fonética (<rPh>) no cuenta.
        partes = [t.text or "" for t in si.findall("m:t", _NS)]
        partes += [t.text or "" for t in si.findall("m:r/m:t", _NS)]
        cadenas.append("".join(partes))
    return cadenas


def _hojas_del_libro(archivo: zipfile.ZipFile) -> list[tuple[str, str]]:
    """(nombre, ruta dentro del zip) de cada hoja, en el orden del libro."""
    libro = ElementTree.fromstring(_leer_miembro(archivo, "xl/workbook.xml"))
    relaciones = ElementTree.fromstring(_leer_miembro(archivo, "xl/_rels/workbook.xml.rels"))
    destinos = {rel.get("Id"): rel.get("Target", "") for rel in relaciones.findall("p:Relationship", _NS)}
    hojas = []
    for hoja in libro.findall("m:sheets/m:sheet", _NS):
        destino = destinos.get(hoja.get("{%s}id" % _NS["r"]), "")
        if not destino:
            continue
        ruta = destino.lstrip("/") if destino.startswith("/") else f"xl/{destino}"
        hojas.append((hoja.get("name", ""), ruta))
    return hojas


def _celdas_de_hoja(archivo: zipfile.ZipFile, ruta: str, cadenas: list[str]) -> list[list[str]]:
    info = archivo.getinfo(ruta)
    if info.file_size > _MAX_MEMBER_BYTES:
        raise ArchivoInvalido("El Excel es demasiado grande para leerlo.")
    filas: list[list[str]] = []
    with archivo.open(info) as flujo:
        for _, elemento in ElementTree.iterparse(flujo, events=("end",)):
            if elemento.tag != f"{_M}row":
                continue
            fila: list[str] = []
            for celda in elemento.findall(f"{_M}c"):
                columna = _indice_de_columna(celda.get("r", ""))
                if columna < 0 or columna >= _MAX_COLUMNAS:
                    continue
                tipo = celda.get("t")
                if tipo == "inlineStr":
                    valor = "".join(t.text or "" for t in celda.iter(f"{_M}t"))
                else:
                    crudo = celda.findtext(f"{_M}v") or ""
                    if tipo == "s":
                        try:
                            valor = cadenas[int(crudo)]
                        except (ValueError, IndexError):
                            valor = ""
                    else:
                        valor = crudo
                if len(fila) <= columna:
                    fila.extend([""] * (columna + 1 - len(fila)))
                fila[columna] = valor
            filas.append(fila)
            elemento.clear()
            if len(filas) >= _MAX_FILAS:
                break
    return filas


def _hojas_xlsx(contenido: bytes) -> list[tuple[str, list[list[str]]]]:
    try:
        archivo = zipfile.ZipFile(io.BytesIO(contenido))
    except zipfile.BadZipFile as exc:
        raise ArchivoInvalido("El archivo no es un Excel (.xlsx) válido.") from exc
    with archivo:
        try:
            cadenas = _cadenas_compartidas(archivo)
            hojas = _hojas_del_libro(archivo)
        except (KeyError, ElementTree.ParseError) as exc:
            raise ArchivoInvalido("El archivo no es un Excel (.xlsx) válido.") from exc

        # Primero la pestaña «Proyección DPO - fecha»; si no está, se prueban todas.
        def prioridad(hoja: tuple[str, str]) -> int:
            nombre = plegar(hoja[0])
            if nombre.startswith("proyeccion dpo"):
                return 0
            return 1 if "proyeccion" in nombre else 2

        resultado = []
        for nombre, ruta in sorted(hojas, key=prioridad):
            try:
                resultado.append((nombre, _celdas_de_hoja(archivo, ruta, cadenas)))
            except (KeyError, ElementTree.ParseError):
                continue
        return resultado


def _hojas_csv(contenido: bytes) -> list[tuple[str, list[list[str]]]]:
    texto = None
    for codificacion in ("utf-8-sig", "cp1252"):
        try:
            texto = contenido.decode(codificacion)
            break
        except UnicodeDecodeError:
            continue
    if texto is None:
        texto = contenido.decode("latin-1", errors="replace")
    muestra = texto[:20000]
    delimitador = ";" if muestra.count(";") > muestra.count(",") else ","
    lector = csv.reader(io.StringIO(texto), delimiter=delimitador)
    filas = []
    for fila in lector:
        filas.append(fila[:_MAX_COLUMNAS])
        if len(filas) >= _MAX_FILAS:
            break
    return [("", filas)]


# ---------------------------------------------------------------------------
# Celdas → filas de la proyección
# ---------------------------------------------------------------------------


def _ubicar_columnas(filas: list[list[str]]) -> tuple[int, dict[str, int]] | None:
    """La fila del encabezado y dónde está cada columna, o None si la hoja no es la proyección."""
    for indice, fila in enumerate(filas[:_FILAS_PARA_ENCABEZADO]):
        nombres = [plegar(celda) for celda in fila]
        if not {"pais", "cliente", "descripcion"} <= set(nombres):
            continue
        columnas = {
            "pais": nombres.index("pais"),
            "cliente": nombres.index("cliente"),
            "descripcion": nombres.index("descripcion"),
        }
        for posicion, nombre in enumerate(nombres):
            compacto = nombre.replace(" ", "")
            if nombre == "status" and "status" not in columnas:
                columnas["status"] = posicion
            elif nombre == "producto" and "producto" not in columnas:
                columnas["producto"] = posicion
            elif re.match(r"^monto \d{4}", nombre) and "monto" not in columnas:
                columnas["monto"] = posicion
            elif compacto in ("fechacci-ido", "fechacci/ido") and "fecha" not in columnas:
                columnas["fecha"] = posicion
        columnas.setdefault("monto", _COL_MONTO)
        columnas.setdefault("producto", _COL_PRODUCTO)
        return indice, columnas
    return None


def _monto(valor: str) -> Decimal | None:
    limpio = re.sub(r"[^\d,.\-]", "", str(valor or ""))
    if not re.search(r"\d", limpio):
        return None
    # «1,234.5» (formato de la planilla) o «1234,5».
    if "," in limpio and "." in limpio:
        limpio = limpio.replace(",", "")
    elif "," in limpio:
        limpio = limpio.replace(",", ".")
    try:
        return Decimal(limpio).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


_EPOCA_EXCEL = datetime(1899, 12, 30)


def _fecha(valor: str) -> tuple[str, date | None]:
    """El texto a mostrar y la fecha, si la celda trae una."""
    texto = str(valor or "").strip()
    if not texto:
        return "", None
    # En el .xlsx una fecha es un número de serie.
    if re.fullmatch(r"\d{5}(\.\d+)?", texto):
        serie = float(texto)
        if 30000 < serie < 80000:
            fecha = (_EPOCA_EXCEL + timedelta(days=serie)).date()
            return fecha.strftime("%d/%m/%Y"), fecha
    for formato in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            fecha = datetime.strptime(texto.split(" ")[0], formato).date()
            return fecha.strftime("%d/%m/%Y"), fecha
        except ValueError:
            continue
    return texto[:60], None


def _celda(fila: list[str], posicion: int | None) -> str:
    if posicion is None or posicion >= len(fila):
        return ""
    return re.sub(r"\s+", " ", str(fila[posicion] or "")).strip()


def _filas_de_hoja(filas: list[list[str]]) -> list[FilaProyeccion] | None:
    ubicacion = _ubicar_columnas(filas)
    if ubicacion is None:
        return None
    encabezado, columnas = ubicacion
    resultado: list[FilaProyeccion] = []
    for fila in filas[encabezado + 1 :]:
        pais = _celda(fila, columnas["pais"])
        cliente = _celda(fila, columnas["cliente"])
        descripcion = _celda(fila, columnas["descripcion"])
        if not (pais or cliente or descripcion):
            continue
        texto_fecha, fecha = _fecha(_celda(fila, columnas.get("fecha")))
        resultado.append(
            FilaProyeccion(
                status=_celda(fila, columnas.get("status"))[:120],
                pais=pais[:120],
                cliente=cliente[:500],
                descripcion=descripcion,
                producto=_celda(fila, columnas["producto"])[:255],
                monto_usd_mm=_monto(_celda(fila, columnas["monto"])),
                fecha_cci_ido_texto=texto_fecha,
                fecha_cci_ido=fecha,
            )
        )
    return resultado


def leer_proyeccion(nombre: str, contenido: bytes) -> LecturaProyeccion:
    """Las operaciones del archivo. Levanta ``ArchivoInvalido`` si no es una proyección."""
    if len(contenido) > MAX_BYTES:
        raise ArchivoInvalido("El archivo pesa más de 15 MB.")
    extension = (nombre or "").lower().rsplit(".", 1)[-1]
    if extension == "csv":
        hojas = _hojas_csv(contenido)
    elif extension in ("xlsx", "xlsm"):
        hojas = _hojas_xlsx(contenido)
    elif extension == "xls":
        raise ArchivoInvalido("El formato .xls antiguo no se puede leer: guardalo como .xlsx o .csv.")
    else:
        raise ArchivoInvalido("Subí la proyección como Excel (.xlsx) o CSV.")

    for nombre_hoja, celdas in hojas:
        filas = _filas_de_hoja(celdas)
        if filas:
            return LecturaProyeccion(hoja=nombre_hoja, filas=filas)
    raise ArchivoInvalido(
        "No se encontró la tabla de la proyección: tiene que haber una fila de "
        "encabezados con «País», «Cliente» y «Descripción»."
    )
