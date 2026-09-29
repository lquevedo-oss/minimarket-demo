"""
Lectura de facturas electrónicas SII en PDF.
Estrategia en cascada:
 1. Buscar XML DTE embebido en el PDF (lo más confiable).
 2. Si no hay, extraer texto y parsear con heurísticas / regex.
Devuelve dict normalizado:
 { folio, rut_emisor, razon_social, fecha, neto, iva, total, items:[{codigo,nombre,cantidad,precio_unit,total}] }
"""
import re
import io
import json
import xml.etree.ElementTree as ET

import pdfplumber


def _strip_ns(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def _find_text(node, name):
    for el in node.iter():
        if _strip_ns(el.tag).lower() == name.lower():
            return (el.text or "").strip()
    return None


def parse_xml_dte(xml_bytes):
    """Parsea un DTE chileno estándar (SII)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    # localizar nodo Encabezado y Detalle
    enc = None
    detalles = []
    for el in root.iter():
        t = _strip_ns(el.tag)
        if t == "Encabezado":
            enc = el
        if t == "Detalle":
            detalles.append(el)
    if enc is None:
        return None

    data = {
        "folio": _find_text(enc, "Folio"),
        "rut_emisor": _find_text(enc, "RUTEmisor"),
        "razon_social": _find_text(enc, "RznSoc") or _find_text(enc, "RznSocEmisor"),
        "fecha": _find_text(enc, "FchEmis"),
        "neto": _to_num(_find_text(enc, "MntNeto")),
        "iva": _to_num(_find_text(enc, "IVA")),
        "total": _to_num(_find_text(enc, "MntTotal")),
        "items": [],
    }
    for d in detalles:
        item = {
            "codigo": _find_text(d, "VlrCodigo") or _find_text(d, "CdgItem") or "",
            "nombre": _find_text(d, "NmbItem") or "",
            "cantidad": _to_num(_find_text(d, "QtyItem")) or 1,
            "precio_unit": _to_num(_find_text(d, "PrcItem")) or 0,
            "total": _to_num(_find_text(d, "MontoItem")) or 0,
        }
        if item["nombre"]:
            data["items"].append(item)
    return data


def _to_num(s):
    if s is None:
        return None
    s = str(s).strip().replace(".", "").replace(",", ".")
    s = re.sub(r"[^0-9.\-]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def extract_embedded_xml(pdf_path):
    """Busca un DTE XML embebido como attachment en el PDF."""
    try:
        import pikepdf
        pdf = pikepdf.open(pdf_path)
        if "/Names" in pdf.Root and "/EmbeddedFiles" in pdf.Root.Names:
            names = pdf.Root.Names.EmbeddedFiles.Names
            for i in range(0, len(names), 2):
                fspec = names[i + 1]
                stream = fspec.EF.F.read_bytes()
                if b"<DTE" in stream or b"<Documento" in stream:
                    return stream
    except Exception:
        pass
    return None


def parse_text_heuristic(text):
    """Fallback: parsea el texto plano de la factura impresa."""
    data = {"folio": None, "rut_emisor": None, "razon_social": None,
            "fecha": None, "neto": None, "iva": None, "total": None, "items": []}

    m = re.search(r"folio[\s:#nº°]*([0-9]{3,})", text, re.I)
    if m:
        data["folio"] = m.group(1)

    m = re.search(r"(\d{1,2}\.?\d{3}\.?\d{3}\-[\dkK])", text)
    if m:
        data["rut_emisor"] = m.group(1)

    m = re.search(r"(?:neto|afecto)[\s:$]*([\d\.\,]+)", text, re.I)
    if m:
        data["neto"] = _to_num(m.group(1))
    m = re.search(r"i\.?v\.?a\.?[\s:$\(\)%19]*([\d\.\,]{3,})", text, re.I)
    if m:
        data["iva"] = _to_num(m.group(1))
    m = re.search(r"total[\s:$]*([\d\.\,]+)", text, re.I)
    if m:
        data["total"] = _to_num(m.group(1))

    # items: líneas con cantidad + descripción + precios
    for line in text.splitlines():
        # patrón: [cant] descripcion ... [precio] [total]
        lm = re.match(r"^\s*(\d+(?:[.,]\d+)?)\s+(.+?)\s+([\d\.]{3,})\s+([\d\.]{3,})\s*$", line)
        if lm:
            cant = _to_num(lm.group(1))
            nombre = lm.group(2).strip()
            pu = _to_num(lm.group(3))
            tot = _to_num(lm.group(4))
            if nombre and cant and len(nombre) > 2:
                data["items"].append({
                    "codigo": "", "nombre": nombre[:200],
                    "cantidad": cant, "precio_unit": pu, "total": tot,
                })
    return data


def parse_factura(pdf_path):
    """Punto de entrada. Devuelve dict normalizado + 'fuente'."""
    # 1. XML embebido
    xml = extract_embedded_xml(pdf_path)
    if xml:
        d = parse_xml_dte(xml)
        if d and d.get("items"):
            d["fuente"] = "xml_embebido"
            return d

    # 2. ¿El PDF contiene texto XML inline?
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)

    if "<DTE" in text or "<Documento" in text:
        start = text.find("<")
        d = parse_xml_dte(text[start:].encode("utf-8", "ignore"))
        if d and d.get("items"):
            d["fuente"] = "xml_inline"
            return d

    # 3. Heurística sobre texto
    d = parse_text_heuristic(text)
    d["fuente"] = "texto"
    return d


if __name__ == "__main__":
    import sys
    print(json.dumps(parse_factura(sys.argv[1]), indent=2, ensure_ascii=False))
