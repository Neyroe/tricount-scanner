"""
Parsing de tickets de caisse exportés depuis les applis des enseignes.

Objectif : zéro IA, 100 % Python. Les applis (Mon E.Leclerc, etc.) exportent
des PDF avec une couche texte propre — on lit ce texte et on extrait les lignes.

Point d'entrée : parse_receipt(raw: bytes, filename: str) -> dict
Retourne {items: [{category, name, price}], ticket_store, ticket_date, total, warning?}
"""
import io
import csv
import re
from typing import Optional


# ---------------------------------------------------------------------------
# CSV (format E.Leclerc "Export Ticket de Caisse")
# ---------------------------------------------------------------------------

def parse_csv(raw: bytes) -> dict:
    text = _decode(raw)
    reader = csv.reader(io.StringIO(text))
    items = []
    ticket_date = ""
    ticket_store = ""
    SKIP_STARTS = ("rayon", "export", "date", "total", "bon", "reste", "sous")

    for row in reader:
        if len(row) >= 1:
            cell = row[0].strip()
            if cell.lower().startswith("export ticket"):
                ticket_store = cell.replace("Export Ticket de Caisse - ", "").strip()
            if cell.lower().startswith("date du ticket"):
                ticket_date = cell.split(":", 1)[-1].strip()

        if len(row) < 5:
            continue

        rayon = row[0].strip()
        article = row[1].strip()
        total = row[4].strip().replace(",", ".")

        if not rayon or not article:
            continue
        if any(rayon.lower().startswith(s) for s in SKIP_STARTS):
            continue
        if any(article.lower().startswith(s) for s in SKIP_STARTS):
            continue
        try:
            price = float(total)
        except ValueError:
            continue
        if price <= 0:
            continue
        items.append({"category": rayon.strip(), "name": article.strip(), "price": price})

    return {
        "items": items,
        "ticket_store": ticket_store,
        "ticket_date": ticket_date,
        "total": round(sum(i["price"] for i in items), 2),
    }


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def parse_pdf(raw: bytes) -> dict:
    import pdfplumber  # imported lazily so the app runs even if absent

    pages_text = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for page in pdf.pages:
            pages_text.append(page.extract_text() or "")
    text = "\n".join(pages_text)

    if not text.strip():
        raise ValueError(
            "Ce PDF ne contient pas de texte lisible (probablement une image scannée). "
            "Exportez un PDF depuis l'appli de l'enseigne."
        )

    store = _detect_store(text)
    if store == "leclerc":
        return _parse_leclerc(text)
    return _parse_generic(text)


def _detect_store(text: str) -> Optional[str]:
    low = text.lower()
    if "leclerc" in low:
        return "leclerc"
    if "lidl" in low:
        return "lidl"
    if "carrefour" in low:
        return "carrefour"
    if "auchan" in low:
        return "auchan"
    return None


# E.Leclerc: lignes "[* ]NOM  PRIX  CODE_TVA" entre l'en-tête "TTC TVA" et "Total N articles"
_LECLERC_ITEM = re.compile(r"^(?:\*\s+)?(.+?)\s+(\d+[.,]\d{2})\s+(\d)$")
_LECLERC_TOTAL = re.compile(r"Total\s+\d+\s+articles?\s+(\d+[.,]\d{2})", re.IGNORECASE)
# code TVA E.Leclerc -> libellé de rayon approximatif (1 = alimentaire 5.5%, 3 = 20%)
_LECLERC_TVA_CAT = {"1": "Alimentaire", "3": "Autres"}


def _parse_leclerc(text: str) -> dict:
    lines = text.split("\n")
    items = []
    in_items = False

    for ln in lines:
        s = ln.strip()
        if re.search(r"\bTTC\b.*\bTVA\b", s):
            in_items = True
            continue
        if not in_items:
            continue
        if s.startswith("---") or s.lower().startswith("total") or s.startswith("CB"):
            break
        m = _LECLERC_ITEM.match(s)
        if not m:
            continue
        name = m.group(1).strip()
        price = float(m.group(2).replace(",", "."))
        tva = m.group(3)
        if price <= 0:
            continue
        items.append({
            "category": _LECLERC_TVA_CAT.get(tva, "Divers"),
            "name": name,
            "price": price,
        })

    date = ""
    dm = re.search(r"(\d{1,2}\s+\w+\s+\d{4}\s+\d{1,2}:\d{2})", text)
    if dm:
        date = dm.group(1)

    result = {
        "items": items,
        "ticket_store": "E.Leclerc",
        "ticket_date": date,
        "total": round(sum(i["price"] for i in items), 2),
    }

    # Cross-check against the printed total
    tm = _LECLERC_TOTAL.search(text)
    if tm:
        printed = float(tm.group(1).replace(",", "."))
        result["printed_total"] = printed
        if abs(printed - result["total"]) > 0.01:
            result["warning"] = (
                f"Somme des articles ({result['total']:.2f}€) "
                f"≠ total du ticket ({printed:.2f}€). Vérifiez les lignes."
            )

    return result


# Generic best-effort: any "NAME .... 12,34" line, skipping obvious non-items.
_GENERIC_ITEM = re.compile(r"^(.+?)\s+(\d+[.,]\d{2})(?:\s*[A-Z€]?)?$")
_GENERIC_SKIP = re.compile(
    r"total|sous.?total|^cb$|carte|montant|tva|espece|rendu|monnaie|"
    r"remise|reduction|rabais|^ht$|^ttc$|caisse|ticket|siret|^code|^total",
    re.IGNORECASE,
)


def _parse_generic(text: str) -> dict:
    items = []
    for ln in text.split("\n"):
        s = ln.strip()
        if not s or _GENERIC_SKIP.search(s):
            continue
        m = _GENERIC_ITEM.match(s)
        if not m:
            continue
        name = m.group(1).strip(" .-")
        try:
            price = float(m.group(2).replace(",", "."))
        except ValueError:
            continue
        # Filter noise: name must have a couple letters and a plausible price
        if price <= 0 or price > 2000 or len(re.sub(r"[^A-Za-zÀ-ÿ]", "", name)) < 2:
            continue
        items.append({"category": "Divers", "name": name, "price": price})

    return {
        "items": items,
        "ticket_store": _detect_store(text) or "",
        "ticket_date": "",
        "total": round(sum(i["price"] for i in items), 2),
        "warning": "Format non reconnu — extraction générique, vérifiez les lignes.",
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF", b"BM")


def parse_receipt(raw: bytes, filename: str) -> dict:
    name = (filename or "").lower()

    if name.endswith(".pdf") or raw[:5] == b"%PDF-":
        return parse_pdf(raw)

    is_image = (
        name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"))
        or raw[:4] in _IMAGE_MAGIC
        or raw[:2] == b"BM"
    )
    if is_image:
        try:
            import receipt_ocr
        except ImportError:
            raise ValueError(
                "L'OCR image n'est pas disponible (easyocr non installé). "
                "Utilisez un export PDF ou CSV."
            )
        return receipt_ocr.parse_image(raw)

    return parse_csv(raw)


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("Encodage du fichier non reconnu")
