"""
actualizar_pisos.py  v3
=======================
Actualización automática de departamentos en renta – Guadalajara
Portales: propiedades.com · EasyBroker · Inmuebles24 · Lamudi · Vivanuncios

MATRIZ DE PUNTUACIÓN (base 115 + bonus hasta ~49)
  A Desplazamiento  28  (min. en bici; ciclovía Yaquis ×0.80, Hidalgo ×0.75)
  B Tamaño          20  (lineal 34 m² → 120 m²)
  C Calidad         10  (2rec, A/C, estado, distribución)
  D Extras          15  (piscina 8, balcón 3, roof 2, gym 2, lavand. 2, cowork 1)
  E Barrio          22  (Americana/Lafayette 22 → Monraz 3)
  F Precio          20  (renta efectiva $14,500 → $21,500; semi +3)
  Bonus 2 factores  hasta +19
  Bonus 3 factores  hasta +38

COMPORTAMIENTO
  · Anuncios CAÍDOS      → se eliminan del mapa y de la tabla
  · Anuncios NO VERIF.   → siguen apareciendo con borde naranja discontinuo
  · Cada ejecución       → regenera mapa HTML y PDF de análisis

USO
    python actualizar_pisos.py                  # completo
    python actualizar_pisos.py --solo-verificar # sin buscar nuevos
    python actualizar_pisos.py --sin-pdf        # omite el PDF (más rápido)

REQUISITOS
    pip install playwright patchright requests beautifulsoup4 lxml reportlab
    patchright install chromium
"""

import json, time, re, sys, datetime, math
from pathlib import Path

CARPETA   = Path(__file__).parent
ROWS_FILE = CARPETA / "rows.json"
MAPA_FILE = CARPETA / "Mapa_departamentos_Guadalajara.html"
PDF_FILE  = CARPETA / "Analisis_departamentos_Guadalajara.pdf"
LOG_FILE  = CARPETA / "actualizaciones.log"
CAND_FILE = CARPETA / "nuevos_candidatos.json"

# ══════════════════════════════════════════════════════════════════════════
# CRITERIOS DE BÚSQUEDA
# ══════════════════════════════════════════════════════════════════════════
PRECIO_MAX_AMUEBLADO = 21000
PRECIO_MAX_SIN_AMUE  = 19500   # filtro duro si no tiene piscina ni 2 rec

COLONIAS_OBJETIVO = [
    "providencia", "ladrón de guevara", "ladron de guevara", "americana",
    "santa teresita", "vallarta norte", "lafayette", "prados de providencia",
    "arcos vallarta", "lomas de guevara",
]

# ══════════════════════════════════════════════════════════════════════════
# MATRIZ DE PUNTUACIÓN
# ══════════════════════════════════════════════════════════════════════════
BARRIO = {
    "Lafayette": 22, "Americana": 22,
    "Ladrón de Guevara": 18, "Arcos Vallarta": 14,
    "Santa Teresita": 13, "Prados de Providencia": 13,
    "Providencia": 11, "Lomas de Guevara": 9, "Italia Providencia": 9,
    "Ayuntamiento": 6, "Monraz": 3,
}

CICLOVIA_YAQUIS  = 0.80   # Av. México → Av. Yaquis
CICLOVIA_HIDALGO = 0.75   # Av. Hidalgo → Lafayette

def coste_amoblar(m2):
    """Coste mensual de amoblar, con 50% de margen, amortizado a 12 meses."""
    if m2 <= 50:   return 2500
    if m2 <= 80:   return 3750
    return 5625

def renta_efectiva(r):
    if r.get("mob") in ("A", "S"):
        return r["pr"]
    return r["pr"] + coste_amoblar(r["m2"])

def score_A(r):
    """Desplazamiento (28 pts). Minutos reales, reducidos si hay ciclovía."""
    mins = r.get("bike", 15)
    if r.get("ciclovia") == "yaquis":  mins *= CICLOVIA_YAQUIS
    if r.get("ciclovia") == "hidalgo": mins *= CICLOVIA_HIDALGO
    return max(0, min(28, round(28 * (15 - mins) / 12)))

def score_B(r):
    """Tamaño (20 pts). Lineal 34 m² → 120 m², techo en 120."""
    return max(0, min(20, round(20 * (r["m2"] - 34) / 86)))

def score_C(r):
    """Calidad del espacio (10 pts)."""
    c = r.get("calidad", {})
    pts = 0
    if r.get("rec", 1) >= 2:   pts += 3
    if r.get("rec", 1) >= 3:   pts += 1
    if c.get("ac"):            pts += 2
    if c.get("estado_nuevo"):  pts += 2
    if c.get("dist_sep"):      pts += 2
    return min(10, pts)

def score_D(r):
    """Extras (15 pts). Piscina domina con 8."""
    ex = r.get("extras", {})
    pts = 0
    if ex.get("pool"):       pts += 8
    if ex.get("balcon"):     pts += 3
    if ex.get("rooftop"):    pts += 2
    if ex.get("gym"):        pts += 2
    if ex.get("lavanderia"): pts += 2
    if ex.get("coworking"):  pts += 1
    return min(15, pts)

def score_E(r):
    """Barrio (22 pts)."""
    return BARRIO.get(r.get("c", ""), 6)

def score_F(r):
    """Precio / calidad-precio (20 pts). Lineal sobre renta efectiva."""
    er   = renta_efectiva(r)
    base = max(0, round(20 * (21500 - er) / 7000))
    if r.get("mob") == "S":
        base += 3
    return min(20, base)

def bonus_2_factores(A, B, C, D, E, F):
    b = []
    if A >= 20 and E >= 18: b.append(("Cerca + Barrio top", 6))
    if B >= 15 and C >= 7:  b.append(("Grande + Bien equipado", 4))
    if D >= 10 and E >= 18: b.append(("Piscina + Barrio top", 5))
    if F >= 14 and B >= 15: b.append(("Precio / espacio", 4))
    return b

def bonus_3_factores(A, B, C, D, E, F):
    b = []
    if A >= 8  and E >= 18 and D >= 8:  b.append(("Cerca + Barrio + Piscina", 12))
    if B >= 14 and C >= 6  and F >= 12: b.append(("Grande + Calidad + Precio", 10))
    if A >= 18 and B >= 14 and E >= 11: b.append(("Cerca + Grande + Barrio", 8))
    if D >= 8  and E >= 11 and F >= 10: b.append(("Piscina + Barrio + Precio", 8))
    return b

def etiqueta_xp(r, E, D):
    """Alto valor experiencial: barrio premier + amenidad principal."""
    if E == 22 and D >= 8:
        return "Barrio premier + piscina"
    if E == 22 and r.get("mob") == "A":
        return "Barrio premier + amueblado"
    if E >= 18 and D >= 10:
        return "Barrio top + amenidades completas"
    return ""

def excluir_por_precio(r):
    """Filtro duro: sin amueblar, sin piscina, sin 2 rec y > $19,500 efectivo."""
    ex = r.get("extras", {})
    if r.get("mob") in ("N", "?") and not ex.get("pool") \
       and r.get("rec", 1) < 2 and renta_efectiva(r) > PRECIO_MAX_SIN_AMUE:
        return True
    return False

def puntuar(r):
    """Calcula todos los factores y el total de una propiedad."""
    A, B, C = score_A(r), score_B(r), score_C(r)
    D, E, F = score_D(r), score_E(r), score_F(r)
    base = A + B + C + D + E + F
    b2   = bonus_2_factores(A, B, C, D, E, F)
    b3   = bonus_3_factores(A, B, C, D, E, F)
    bpts = sum(v for _, v in b2) + sum(v for _, v in b3)
    r.update({
        "sA": A, "sB": B, "sC": C, "sD": D, "sE": E, "sF": F,
        "base": base, "bonus2": b2, "bonus3": b3,
        "bpts": bpts, "tot": base + bpts,
        "eff_rent": renta_efectiva(r),
        "xp": etiqueta_xp(r, E, D),
    })
    return r

# ══════════════════════════════════════════════════════════════════════════
# UTILIDADES
# ══════════════════════════════════════════════════════════════════════════
def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    linea = f"[{ts}] {msg}"
    print(linea)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(linea + "\n")
    except Exception:
        pass

def cargar_rows():
    if ROWS_FILE.exists():
        return json.loads(ROWS_FILE.read_text(encoding="utf-8"))
    return []

def guardar_rows(rows):
    ROWS_FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                         encoding="utf-8")

SENALES_MUERTO = [
    "no encontrado", "anuncio eliminado", "anuncio no disponible",
    "anuncio expirado", "ya no está disponible", "no longer available",
    "listing not found", "inmueble no disponible", "propiedad no disponible",
    "esta propiedad ya fue", "este anuncio ya no", "page not found",
    "error 404", "404 not found", "fue eliminado", "propiedad eliminada",
]

def esta_muerto(texto):
    t = texto.lower()
    return any(s in t for s in SENALES_MUERTO)

def en_colonia_objetivo(texto):
    t = (texto or "").lower()
    return any(c in t for c in COLONIAS_OBJETIVO)

# ══════════════════════════════════════════════════════════════════════════
# VERIFICACIÓN
# ══════════════════════════════════════════════════════════════════════════
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")

def verificar_http(url):
    import requests
    try:
        r = requests.get(url, timeout=12, allow_redirects=True, headers={
            "User-Agent": UA, "Accept-Language": "es-MX,es;q=0.9"})
        if r.status_code == 404:
            return "caido"
        if r.status_code == 200:
            return "caido" if esta_muerto(r.text) else "activo"
        return "no_verificable"
    except Exception:
        return "no_verificable"

def crear_browser(p):
    browser = p.chromium.launch(headless=True,
                                args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = browser.new_context(
        viewport={"width": 1366, "height": 768},
        locale="es-MX", user_agent=UA,
        extra_http_headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.7"})
    return browser, ctx

def verificar_stealth(page, url):
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=22000)
        time.sleep(1.5)
        if resp and resp.status == 404:
            return "caido"
        try:
            texto = page.inner_text("body")
        except Exception:
            texto = ""
        if len(texto.strip()) < 80:
            return "no_verificable"
        return "caido" if esta_muerto(texto) else "activo"
    except Exception:
        return "no_verificable"

# ══════════════════════════════════════════════════════════════════════════
# BÚSQUEDA DE NUEVOS
# ══════════════════════════════════════════════════════════════════════════
def buscar_propiedades_com(urls_existentes):
    import requests
    from bs4 import BeautifulSoup
    nuevos = []
    url = (f"https://propiedades.com/guadalajara/departamentos-renta"
           f"?precio-maximo={PRECIO_MAX_AMUEBLADO}&orden=reciente")
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": UA})
        if r.status_code != 200:
            log(f"    propiedades.com: HTTP {r.status_code}")
            return nuevos
        soup = BeautifulSoup(r.text, "lxml")
        for card in soup.select("article, div[class*='property'], div[class*='listing']")[:40]:
            try:
                a = card.select_one("a[href*='/inmuebles/']")
                if not a: continue
                href = a.get("href", "")
                u = "https://propiedades.com" + href if href.startswith("/") else href
                if u in urls_existentes: continue
                pe = card.select_one("[class*='price'],[class*='precio']")
                ptxt = pe.get_text(strip=True) if pe else ""
                precio = int(re.sub(r"[^\d]", "", ptxt)) if re.search(r"\d{4,}", ptxt) else 0
                if not (5000 < precio <= PRECIO_MAX_AMUEBLADO): continue
                ne = card.select_one("h2,h3,[class*='title']")
                nombre = ne.get_text(strip=True)[:60] if ne else "Departamento"
                ce = card.select_one("[class*='location'],[class*='colonia']")
                colonia = ce.get_text(strip=True) if ce else ""
                if not en_colonia_objetivo(colonia + " " + nombre): continue
                nuevos.append({"nombre": nombre, "colonia": colonia, "precio": precio,
                               "url": u, "portal": "propiedades.com"})
            except Exception:
                continue
        log(f"    propiedades.com: {len(nuevos)} candidatos")
    except Exception as e:
        log(f"    propiedades.com: error – {e}")
    return nuevos

def buscar_easybroker(urls_existentes):
    import requests
    nuevos = []
    try:
        r = requests.get("https://api.easybroker.com/v1/properties",
            params={"property_types[]": "apartment", "operation_type": "rental",
                    "location": "Guadalajara, Jalisco",
                    "max_price": PRECIO_MAX_AMUEBLADO, "currency": "MXN",
                    "page": 1, "per_page": 50},
            headers={"X-Authorization": "key_Rw7oZKFUb8gxdD2jcfvn",
                     "Accept": "application/json"}, timeout=12)
        if r.status_code != 200:
            log(f"    EasyBroker: HTTP {r.status_code}")
            return nuevos
        for prop in r.json().get("content", [])[:40]:
            try:
                u = prop.get("share_link", "")
                if not u or u in urls_existentes: continue
                ops = prop.get("operations", [])
                precio = ops[0].get("amount", 0) if ops else 0
                if not (5000 < precio <= PRECIO_MAX_AMUEBLADO): continue
                colonia = prop.get("location", {}).get("name", "")
                if not en_colonia_objetivo(colonia): continue
                nuevos.append({
                    "nombre": prop.get("title", "Departamento")[:60],
                    "colonia": colonia, "precio": precio,
                    "m2": prop.get("construction_size", 0),
                    "rec": prop.get("bedrooms", 1),
                    "url": u, "portal": "EasyBroker"})
            except Exception:
                continue
        log(f"    EasyBroker: {len(nuevos)} candidatos")
    except Exception as e:
        log(f"    EasyBroker: error – {e}")
    return nuevos

def buscar_stealth_portal(page, nombre, url_busqueda, urls_existentes):
    from bs4 import BeautifulSoup
    nuevos = []
    try:
        page.goto(url_busqueda, wait_until="domcontentloaded", timeout=28000)
        time.sleep(3)
        soup = BeautifulSoup(page.content(), "lxml")
        if "inmuebles24" in url_busqueda:
            sel_card = ("div[class*='posting-card'], article[class*='posting'], "
                        "div[class*='CardContainer']")
            sel_link, dominio = "a[href*='/propiedades/']", "https://www.inmuebles24.com"
        else:
            sel_card = ("div[class*='ListingCell'], article[class*='listing'], "
                        "div[class*='card']")
            sel_link, dominio = "a[href*='/detalle/'], a[href*='/jalisco/']", "https://www.lamudi.com.mx"
        for card in soup.select(sel_card)[:25]:
            try:
                a = card.select_one(sel_link)
                if not a: continue
                href = a.get("href", "")
                u = dominio + href if href.startswith("/") else href
                if u in urls_existentes: continue
                pe = card.select_one("[class*='price'],[class*='Price']")
                ptxt = pe.get_text(strip=True) if pe else ""
                precio = int(re.sub(r"[^\d]", "", ptxt)) if re.search(r"\d{4,}", ptxt) else 0
                if not (5000 < precio <= PRECIO_MAX_AMUEBLADO): continue
                ne = card.select_one("h2,h3,[class*='title']")
                titulo = ne.get_text(strip=True)[:60] if ne else "Departamento"
                nuevos.append({"nombre": titulo, "precio": precio,
                               "url": u, "portal": nombre})
            except Exception:
                continue
        log(f"    {nombre}: {len(nuevos)} candidatos")
    except Exception as e:
        log(f"    {nombre}: error – {e}")
    return nuevos

# ══════════════════════════════════════════════════════════════════════════
# REGENERAR MAPA
# ══════════════════════════════════════════════════════════════════════════
def regenerar_mapa(rows_vivos):
    if not MAPA_FILE.exists():
        log("  AVISO: no se encontró el mapa HTML en esta carpeta")
        return False
    payload = json.dumps(rows_vivos, ensure_ascii=False)
    html = MAPA_FILE.read_text(encoding="utf-8")
    nuevo = re.sub(r"var D=\[.*?\];(?=\s*var S)", f"var D={payload};",
                   html, count=1, flags=re.DOTALL)
    if nuevo == html:
        nuevo = re.sub(r"var D\s*=\s*\[.*?\];", f"var D={payload};",
                       html, count=1, flags=re.DOTALL)
    if nuevo == html:
        log("  AVISO: no se pudo inyectar el payload en el mapa")
        return False
    fecha = datetime.date.today().strftime("%d/%m/%Y %H:%M")
    banner = (f'<span style="background:#1a7a3c;color:#fff;font-size:10px;'
              f'padding:2px 8px;border-radius:3px;margin-left:8px">'
              f'Actualizado {fecha}</span>')
    nuevo = re.sub(r'(<span id="cnt"></span>)(?!<span style="background:#1a7a3c)',
                   r"\1" + banner, nuevo, count=1)
    MAPA_FILE.write_text(nuevo, encoding="utf-8")
    log(f"  Mapa actualizado con {len(rows_vivos)} propiedades")
    return True

# ══════════════════════════════════════════════════════════════════════════
# REGENERAR PDF
# ══════════════════════════════════════════════════════════════════════════
def regenerar_pdf(rows_vivos):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                        Table, TableStyle, PageBreak, HRFlowable)
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_LEFT, TA_JUSTIFY
    except ImportError:
        log("  AVISO: reportlab no instalado – omitiendo PDF")
        log("         instala con: pip install reportlab")
        return False

    MOB_T = {"A": "Amueblado", "S": "Semi", "N": "Sin amue.", "?": "S/D"}
    EX_LBL = {"pool": "Piscina", "ac": "A/C", "balcon": "Balcon",
              "lavanderia": "Lavand.", "rooftop": "Roof", "gym": "Gym",
              "coworking": "Cowork", "sin_aval": "Sin aval"}

    def ex_str(r):
        ex = r.get("extras", {})
        return ", ".join(EX_LBL[k] for k in EX_LBL if ex.get(k)) or "-"

    doc = SimpleDocTemplate(str(PDF_FILE), pagesize=A4,
        leftMargin=14*mm, rightMargin=14*mm, topMargin=13*mm, bottomMargin=13*mm,
        title="Analisis departamentos Guadalajara", author="Andoni Urtasun")
    ss = getSampleStyleSheet()
    def PS(n, **k): return ParagraphStyle(n, parent=ss["Normal"], **k)
    kick = PS("k", fontName="Helvetica-Bold", fontSize=8,
              textColor=colors.HexColor("#555"), spaceAfter=2)
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontName="Helvetica-Bold",
                        fontSize=21, leading=25, alignment=TA_LEFT, spaceAfter=4)
    h2 = PS("h2", fontName="Helvetica-Bold", fontSize=13, leading=17,
            spaceBefore=8, spaceAfter=4, textColor=colors.HexColor("#1a3a5c"))
    body = PS("b", fontSize=8.5, leading=12.5, spaceAfter=4, alignment=TA_JUSTIFY)
    note = PS("n", fontSize=7.5, leading=11, textColor=colors.HexColor("#555"),
              spaceAfter=3)
    capt = PS("c", fontSize=7, textColor=colors.HexColor("#888"), spaceAfter=6)
    LINE = colors.HexColor("#ccc")
    PURPLE = colors.HexColor("#f3eaf8")

    story = [
        Paragraph("ANALISIS DE MERCADO - GUADALAJARA", kick),
        Paragraph("Departamentos en renta<br/>Ranking y analisis", h1),
        Paragraph(f"{len(rows_vivos)} propiedades activas - Canadian School "
                  f"(Av. Montevideo 3500) - actualizado "
                  f"{datetime.date.today().strftime('%d/%m/%Y')}", note),
        HRFlowable(width="100%", thickness=1,
                   color=colors.HexColor("#1a3a5c"), spaceBefore=4, spaceAfter=8),
        Paragraph("Metodologia", h2),
        Paragraph(
            "<b>A Desplazamiento</b> (28 pts): minutos reales en bici. "
            "Ciclovia Av. Mexico/Yaquis reduce el tiempo un 20%; Av. Hidalgo un 25%. "
            "<b>B Tamano</b> (20 pts): lineal de 34 m2 a 120 m2, techo en 120. "
            "<b>C Calidad</b> (10 pts): recamaras, A/C, estado, distribucion. "
            "<b>D Extras</b> (15 pts): piscina 8, balcon 3, rooftop 2, gym 2, "
            "lavanderia 2, coworking 1. "
            "<b>E Barrio</b> (22 pts): Americana y Lafayette 22, Ladron de Guevara 18, "
            "Santa Teresita y Prados 13, Providencia 11, Ayuntamiento 6, Monraz 3. "
            "<b>F Precio</b> (20 pts): sobre renta efectiva (nominal + coste "
            "mensualizado de amoblar con 50% de margen). "
            "<b>Bonus</b>: combinaciones de 2 factores hasta +19; de 3 factores hasta +38.",
            body),
        Paragraph("Los anuncios caidos se eliminan automaticamente. Los no verificables "
                  "(portales que bloquean acceso o requieren login) se mantienen "
                  "marcados para revision manual.", note),
        PageBreak(),
    ]

    # Tabla completa
    story += [Paragraph("RANKING COMPLETO", kick),
              Paragraph(f"{len(rows_vivos)} propiedades ordenadas por puntuacion", h2)]
    data = [["#", "Departamento", "Barrio", "Renta", "m2", "Rec", "Mob",
             "Bici", "A", "B", "C", "D", "E", "F", "Base", "Bon", "TOT"]]
    for r in rows_vivos:
        data.append([
            str(r["id"]), r["n"][:24], r["c"][:16],
            f"${r['pr']//1000}k", str(r["m2"]), str(r.get("rec", 1)),
            MOB_T.get(r.get("mob", "?"), "?"),
            f"{r.get('bike','?')}'",
            str(r.get("sA", 0)), str(r.get("sB", 0)), str(r.get("sC", 0)),
            str(r.get("sD", 0)), str(r.get("sE", 0)), str(r.get("sF", 0)),
            str(r.get("base", 0)),
            f"+{r['bpts']}" if r.get("bpts") else "-",
            str(r.get("tot", 0)),
        ])
    tw = [8*mm, 40*mm, 24*mm, 12*mm, 8*mm, 8*mm, 15*mm, 10*mm,
          7*mm, 7*mm, 7*mm, 7*mm, 7*mm, 7*mm, 10*mm, 9*mm, 10*mm]
    t = Table(data, colWidths=tw, repeatRows=1)
    st = [("FONTSIZE", (0, 0), (-1, -1), 6),
          ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
          ("FONTNAME", (16, 1), (16, -1), "Helvetica-Bold"),
          ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a3a5c")),
          ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
          ("GRID", (0, 0), (-1, -1), 0.3, LINE),
          ("ALIGN", (0, 0), (-1, -1), "CENTER"),
          ("ALIGN", (1, 0), (2, -1), "LEFT"),
          ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
          ("TOPPADDING", (0, 0), (-1, -1), 2),
          ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
    for i, r in enumerate(rows_vivos, 1):
        if r.get("xp"):
            st.append(("BACKGROUND", (0, i), (-1, i), PURPLE))
        if r.get("available_detail") == "no_verificable":
            st.append(("TEXTCOLOR", (1, i), (1, i), colors.HexColor("#b06000")))
    t.setStyle(TableStyle(st))
    story += [t, Spacer(1, 8)]

    # Top 5 comentado
    story += [Paragraph("TOP 5 COMENTADO", kick),
              Paragraph("Las cinco mejores opciones", h2)]
    for r in rows_vivos[:5]:
        bon = ", ".join(f"{n} (+{v})" for n, v in
                        (r.get("bonus2", []) + r.get("bonus3", []))) or "sin bonus"
        story.append(Paragraph(
            f"<b>{r['id']}. {r['n']}</b> - {r['c']} - ${r['pr']:,} - "
            f"{r['m2']} m2, {r.get('rec',1)} rec, {MOB_T.get(r.get('mob','?'),'?')} - "
            f"{r.get('km','?')} km / {r.get('bike','?')} min en bici. "
            f"Base {r.get('base',0)} + bonus {r.get('bpts',0)} = "
            f"<b>{r.get('tot',0)} pts</b>. Bonus: {bon}. "
            f"Extras: {ex_str(r)}.", body))

    story.append(Paragraph(
        f"Documento generado automaticamente el "
        f"{datetime.datetime.now().strftime('%d/%m/%Y a las %H:%M')}. "
        f"Filas moradas = alto valor experiencial. "
        f"Nombres en naranja = no verificable, revisar manualmente.", capt))

    doc.build(story)
    log(f"  PDF actualizado: {len(rows_vivos)} propiedades")
    return True

# ══════════════════════════════════════════════════════════════════════════
# PROGRAMA PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════
def main():
    solo_verificar = "--solo-verificar" in sys.argv
    sin_pdf        = "--sin-pdf" in sys.argv

    log("=" * 62)
    log(f"ACTUALIZACION {'(solo verificar)' if solo_verificar else '(completa)'}")
    log("=" * 62)

    rows = cargar_rows()
    if not rows:
        log("ERROR: no se encontro rows.json en esta carpeta")
        return

    urls_existentes = {u for r in rows for u in r.get("urls", [])}

    # ── FASE 1: verificar ──────────────────────────────────────────────────
    log("\n-- FASE 1: verificando anuncios existentes --")
    P_HTTP    = ["propiedades.com"]
    P_STEALTH = ["inmuebles24", "lamudi", "vivanuncios"]

    for r in rows:
        u = [x for x in r.get("urls", []) if any(p in x for p in P_HTTP)]
        if not u: continue
        est = verificar_http(u[0])
        r["available"] = (est == "activo")
        r["available_detail"] = est
        r["last_checked"] = datetime.date.today().isoformat()
        ico = {"activo": "OK", "caido": "XX", "no_verificable": "??"}[est]
        log(f"  {ico} {est:15s} [{r['id']:2d}] {r['n'][:34]}")
        time.sleep(0.4)

    rows_st = [r for r in rows
               if any(any(p in u for p in P_STEALTH) for u in r.get("urls", []))
               and not any(any(p in u for p in P_HTTP) for u in r.get("urls", []))]
    if rows_st:
        log(f"\n  Navegador stealth para {len(rows_st)} anuncios...")
        try:
            from patchright.sync_api import sync_playwright as pr
            with pr() as p:
                browser, ctx = crear_browser(p)
                page = ctx.new_page()
                for r in rows_st:
                    u = [x for x in r.get("urls", [])
                         if any(q in x for q in P_STEALTH)]
                    if not u: continue
                    est = verificar_stealth(page, u[0])
                    r["available"] = (est == "activo")
                    r["available_detail"] = est
                    r["last_checked"] = datetime.date.today().isoformat()
                    ico = {"activo": "OK", "caido": "XX", "no_verificable": "??"}[est]
                    log(f"  {ico} {est:15s} [{r['id']:2d}] {r['n'][:34]}")
                    time.sleep(1)
                browser.close()
        except ImportError:
            log("  AVISO: patchright no instalado")
        except Exception as e:
            log(f"  ERROR stealth: {e}")

    for r in rows:
        if "available" not in r:
            r["available"] = None
            r["available_detail"] = "no_verificable"
            r["last_checked"] = datetime.date.today().isoformat()
            log(f"  ?? no_verificable  [{r['id']:2d}] {r['n'][:34]}  (sin URL)")

    # ── FASE 2: eliminar caidos y repuntuar ────────────────────────────────
    log("\n-- FASE 2: recalculando puntuaciones --")
    caidos = [r for r in rows if r.get("available_detail") == "caido"]
    for r in caidos:
        log(f"  ELIMINADO: [{r['id']}] {r['n'][:40]} (anuncio caido)")

    vivos = [r for r in rows if r.get("available_detail") != "caido"]
    excluidos = [r for r in vivos if excluir_por_precio(r)]
    for r in excluidos:
        log(f"  EXCLUIDO por precio: {r['n'][:35]} "
            f"(${r['pr']:,} sin amueblar, sin piscina ni 2 rec)")
    vivos = [r for r in vivos if not excluir_por_precio(r)]

    for r in vivos:
        puntuar(r)
    vivos.sort(key=lambda x: -x["tot"])
    for i, r in enumerate(vivos, 1):
        r["id"] = i
        r["id2"] = i

    log(f"  {len(vivos)} propiedades activas repuntuadas")
    if vivos:
        log(f"  Rango de puntuacion: {vivos[-1]['tot']} - {vivos[0]['tot']}")
        log(f"  Top 3: " + " | ".join(f"{r['n'][:22]} ({r['tot']})"
                                       for r in vivos[:3]))

    # ── FASE 3: buscar nuevos ──────────────────────────────────────────────
    nuevos = []
    if not solo_verificar:
        log("\n-- FASE 3: buscando nuevos anuncios --")
        log(f"  Colonias: Providencia, Ladron de Guevara, Americana, "
            f"Santa Teresita, Vallarta Norte")
        log(f"  Precio maximo: ${PRECIO_MAX_AMUEBLADO:,} amueblado")
        nuevos += buscar_propiedades_com(urls_existentes)
        nuevos += buscar_easybroker(urls_existentes)
        try:
            from patchright.sync_api import sync_playwright as pr
            with pr() as p:
                browser, ctx = crear_browser(p)
                page = ctx.new_page()
                nuevos += buscar_stealth_portal(page, "Inmuebles24",
                    "https://www.inmuebles24.com/departamentos-en-renta-en-"
                    "ladron-de-guevara,americana,providencia,santa-teresita-"
                    f"jalisco.html?precio-maximo={PRECIO_MAX_AMUEBLADO}",
                    urls_existentes)
                nuevos += buscar_stealth_portal(page, "Lamudi",
                    "https://www.lamudi.com.mx/jalisco/guadalajara/for-rent/"
                    f"?price[max]={PRECIO_MAX_AMUEBLADO}&property_type[]=Apartamento",
                    urls_existentes)
                browser.close()
        except Exception as e:
            log(f"    ERROR busqueda stealth: {e}")

        if nuevos:
            prev = []
            if CAND_FILE.exists():
                try: prev = json.loads(CAND_FILE.read_text(encoding="utf-8"))
                except Exception: prev = []
            ya = {c.get("url") for c in prev}
            sin_dup = [n for n in nuevos if n.get("url") not in ya]
            for n in sin_dup:
                n["encontrado"] = datetime.date.today().isoformat()
            CAND_FILE.write_text(
                json.dumps(prev + sin_dup, ensure_ascii=False, indent=2),
                encoding="utf-8")
            log(f"  {len(sin_dup)} candidatos NUEVOS guardados en "
                f"nuevos_candidatos.json")
            log("  -> Abre ese archivo, revisa los anuncios y pasa a Claude")
            log("     los que te interesen para anadirlos al mapa")
        else:
            log("  No se encontraron candidatos nuevos")

    # ── FASE 4: guardar y regenerar ────────────────────────────────────────
    log("\n-- FASE 4: guardando y regenerando --")
    guardar_rows(vivos)
    regenerar_mapa(vivos)
    if not sin_pdf:
        regenerar_pdf(vivos)

    # ── RESUMEN ────────────────────────────────────────────────────────────
    act = sum(1 for r in vivos if r.get("available_detail") == "activo")
    nov = sum(1 for r in vivos if r.get("available_detail") == "no_verificable")
    log("\n" + "=" * 62)
    log("RESUMEN")
    log(f"  Activos verificados : {act}")
    log(f"  No verificables     : {nov}  (revisar manualmente)")
    log(f"  Eliminados (caidos) : {len(caidos)}")
    log(f"  Excluidos por precio: {len(excluidos)}")
    log(f"  Candidatos nuevos   : {len(nuevos)}")
    log(f"  Total en el mapa    : {len(vivos)}")
    log("=" * 62 + "\n")

if __name__ == "__main__":
    main()
