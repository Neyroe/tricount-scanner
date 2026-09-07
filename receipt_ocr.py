"""
OCR de tickets de caisse en image (quand aucun export PDF n'est possible, ex: Lidl).

L'OCR sur image n'est jamais fiable à 100 %. On l'utilise pour *pré-remplir* les
lignes, puis on valide la somme contre le total imprimé ("A payer …") et on
renvoie toujours un avertissement invitant à vérifier.

Approche : une seule passe OCR sur l'image upscalée (x2), reconstruction des
lignes (nom + montants), application des remises (Rabais/Réduction) à l'article
précédent, et validation par le total.

Point d'entrée : parse_image(raw: bytes) -> dict
"""
import io
import re
from collections import Counter
from typing import Optional

import numpy as np
from PIL import Image

_READER = None
_SCALE = 2


def _get_reader():
    """Charge EasyOCR une seule fois (lazy — évite de ralentir le démarrage)."""
    global _READER
    if _READER is None:
        import easyocr
        _READER = easyocr.Reader(["fr"], gpu=False, verbose=False)
    return _READER


_DEC = re.compile(r"-?\d+[.,]\d{2}")            # nombre décimal (prix)
_DISCOUNT = re.compile(r"rabais|r[ée]duction|remise", re.IGNORECASE)
_WEIGHT = re.compile(r"kg\s*[x×]|eur\s*/\s*kg", re.IGNORECASE)
_SKIP = re.compile(
    r"article|p\.?\s*u|qt[ée]|^eur$|tva|total|[àa]\s*payer|carte|montant|"
    r"promotion|[ée]ligible|^ht$|^ttc$|nombre de lignes|sous.?total|"
    r"^\W*$|ticket|siret|^code|^ni:|^gkr|magasin|points?|www|\.fr",
    re.IGNORECASE,
)


def parse_image(raw: bytes) -> dict:
    reader = _get_reader()
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    W, H = im.size

    big = im.resize((W * _SCALE, H * _SCALE), Image.LANCZOS)
    res = reader.readtext(np.array(big), detail=1, paragraph=False,
                          text_threshold=0.5, low_text=0.3)
    if not res:
        raise ValueError("Aucun texte détecté dans l'image.")

    boxes = []
    for box, txt, conf in res:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        boxes.append({
            "y": sum(ys) / 4 / _SCALE,
            "x": min(xs) / _SCALE,
            "txt": txt.strip(),
        })

    rows = _group_rows(boxes)
    y_start, y_end = _region_bounds(rows)

    items = []
    for row in rows:
        yc = sum(b["y"] for b in row) / len(row)
        if yc < y_start or yc > y_end:
            continue

        row.sort(key=lambda b: b["x"])
        text = _recollate(" ".join(b["txt"] for b in row))

        # Ligne de détail au poids ("… kg x … EUR/kg") → jamais un article
        if _WEIGHT.search(text):
            continue

        # Remise → soustraire de l'article précédent (seulement si négatif net lu)
        if _DISCOUNT.search(text):
            disc = _first_decimal(text, negative=True)
            if disc is not None and items:
                items[-1]["price"] = round(max(items[-1]["price"] - abs(disc), 0), 2)
            continue

        name = _extract_name(row)
        if not name or _SKIP.search(name):
            continue
        if len(re.sub(r"[^A-Za-zÀ-ÿ]", "", name)) < 2:
            continue

        price = _line_price(text)
        if price is None or price <= 0:
            continue
        items.append({"category": "Divers", "name": name, "price": round(price, 2)})

    all_text = _recollate("\n".join(b["txt"] for b in boxes))
    printed_total = _find_total(all_text)
    computed = round(sum(i["price"] for i in items), 2)

    warning = "Lecture OCR (image) — vérifiez les prix, ils ne sont pas garantis à 100 %. "
    if printed_total is not None:
        warning += f"Total du ticket : {printed_total:.2f}€."
        if abs(printed_total - computed) > 0.01:
            warning += f" ⚠ Somme lue {computed:.2f}€ — corrigez les lignes en écart."

    return {
        "items": items,
        "ticket_store": "Lidl" if "lidl" in all_text.lower() else "",
        "ticket_date": _find_date(all_text),
        "total": computed,
        "printed_total": printed_total,
        "warning": warning,
    }


def _group_rows(boxes, tol=14):
    ordered = sorted(boxes, key=lambda b: b["y"])
    rows, cur, last_y = [], [], None
    for b in ordered:
        if last_y is None or abs(b["y"] - last_y) <= tol:
            cur.append(b)
        else:
            rows.append(cur)
            cur = [b]
        last_y = b["y"]
    if cur:
        rows.append(cur)
    return rows


def _region_bounds(rows):
    """Zone articles : de l'en-tête (Article/P.U.) jusqu'au total/nb de lignes."""
    y_start, y_end = None, None
    for row in rows:
        text = " ".join(b["txt"] for b in row).lower()
        yc = sum(b["y"] for b in row) / len(row)
        if y_start is None and ("p.u" in text or "article" in text):
            y_start = yc
        if y_start is not None and re.search(r"nombre de lignes|[àa] payer", text):
            y_end = yc
            break
    if y_start is None:
        y_start = 0
    if y_end is None:
        y_end = float("inf")
    return y_start, y_end


def _recollate(s: str) -> str:
    """Recolle les nombres cassés par l'OCR : "1 , 89" -> "1,89"."""
    return re.sub(r"(\d)\s*([.,])\s*(\d)", r"\1\2\3", s)


def _extract_name(row) -> str:
    tokens = []
    for b in row:
        t = _recollate(b["txt"])
        if _DEC.search(t):
            continue
        if re.fullmatch(r"[\d.,\-ATB€ ]+", t):  # nombres, qté, classes TVA
            continue
        tokens.append(b["txt"])
    return " ".join(tokens).strip(" .-:,")


def _line_price(text: str) -> Optional[float]:
    """Prix d'une ligne article : montant positif le plus fréquent (P.U. == EUR)."""
    vals = [float(m.replace(",", ".")) for m in _DEC.findall(text)]
    pos = [v for v in vals if v > 0]
    if not pos:
        return None
    return Counter(pos).most_common(1)[0][0]


def _first_decimal(text: str, negative=False) -> Optional[float]:
    for m in _DEC.findall(text):
        v = float(m.replace(",", "."))
        if negative and v < 0:
            return v
        if not negative and v > 0:
            return v
    return None


def _find_total(text: str) -> Optional[float]:
    m = re.search(r"[àa]\s*payer\s*:?\s*(\d+[.,]\d{2})", text, re.IGNORECASE)
    return float(m.group(1).replace(",", ".")) if m else None


def _find_date(text: str) -> str:
    m = re.search(r"\b(\d{2}[-/.]\d{2}[-/.]\d{2,4})\b", text)
    return m.group(1) if m else ""
