"""
Mode démo — 100 % hors ligne, aucune connexion à Tricount.

Sert la même interface que l'application réelle, mais les appels API sont
remplacés par un Tricount fictif en mémoire (membres, dépenses, soldes).
Le parsing des tickets, lui, est le vrai code de `receipt_parser`.

    python demo/demo_server.py     ->  http://localhost:8010
"""
import sys
from copy import deepcopy
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import receipt_parser  # noqa: E402
from main import _compute_split  # noqa: E402

# ---------------------------------------------------------------------------
# Tricount fictif
# ---------------------------------------------------------------------------

MEMBERS = [
    {"uuid": "m-alex", "name": "Alex"},
    {"uuid": "m-billie", "name": "Billie"},
    {"uuid": "m-charlie", "name": "Charlie"},
]

SEED_TX = [
    {
        "id": 1, "description": "Courses du samedi", "amount": 51.40, "currency": "EUR",
        "payer": "Alex", "payer_uuid": "m-alex", "date": "2026-03-07",
        "category": "GROCERIES",
        "allocations": [
            {"member": "Alex", "amount": 21.10},
            {"member": "Billie", "amount": 16.15},
            {"member": "Charlie", "amount": 14.15},
        ],
        "attachments": [],
    },
    {
        "id": 2, "description": "Plein d'essence", "amount": 62.00, "currency": "EUR",
        "payer": "Billie", "payer_uuid": "m-billie", "date": "2026-03-05",
        "category": "TRANSPORT",
        "allocations": [
            {"member": "Alex", "amount": 20.67},
            {"member": "Billie", "amount": 20.67},
            {"member": "Charlie", "amount": 20.66},
        ],
        "attachments": [],
    },
    {
        "id": 3, "description": "Resto du vendredi", "amount": 78.50, "currency": "EUR",
        "payer": "Charlie", "payer_uuid": "m-charlie", "date": "2026-03-02",
        "category": "FOOD_DRINK",
        "allocations": [
            {"member": "Alex", "amount": 26.17},
            {"member": "Billie", "amount": 26.17},
            {"member": "Charlie", "amount": 26.16},
        ],
        "attachments": [],
    },
]

STATE = {"transactions": deepcopy(SEED_TX), "next_id": 4}


def _balances():
    """Solde net par membre : ce qu'il a avancé moins ce qu'il doit."""
    bal = {m["uuid"]: 0.0 for m in MEMBERS}
    name_to_uuid = {m["name"]: m["uuid"] for m in MEMBERS}
    for tx in STATE["transactions"]:
        bal[tx["payer_uuid"]] = bal.get(tx["payer_uuid"], 0.0) + tx["amount"]
        for alloc in tx["allocations"]:
            uuid = name_to_uuid.get(alloc["member"])
            if uuid:
                bal[uuid] -= alloc["amount"]
    return [
        {"uuid": m["uuid"], "name": m["name"], "balance": round(bal[m["uuid"]], 2)}
        for m in MEMBERS
    ]


# ---------------------------------------------------------------------------
# API mockée (même contrat que main.py)
# ---------------------------------------------------------------------------

app = FastAPI(title="Tricount Scanner — démo")


class ItemAssignment(BaseModel):
    name: str
    price: float
    assigned_to: str


class CreateExpenseRequest(BaseModel):
    tricount_token: str
    payer_uuid: str
    ticket_name: str
    items: list[ItemAssignment]


class _Member:
    def __init__(self, d):
        self.uuid, self.display_name = d["uuid"], d["name"]


@app.get("/api/config")
async def config():
    return {
        "preconfigured": True,
        "token": "demo",
        "name": "Coloc Démo",
        "currency": "EUR",
        "members": MEMBERS,
    }


@app.get("/api/tricount/overview")
async def overview():
    return {
        "name": "Coloc Démo",
        "currency": "EUR",
        "members": _balances(),
        "transactions": sorted(STATE["transactions"], key=lambda t: t["date"], reverse=True),
    }


@app.get("/api/tricount/gallery")
async def gallery():
    return {"attachments": []}


@app.post("/api/parse-csv")
async def parse(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        result = receipt_parser.parse_receipt(raw, file.filename or "")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not result.get("items"):
        raise HTTPException(status_code=422, detail="Aucun article trouvé.")
    return result


@app.post("/api/create-expense")
async def create_expense(request: CreateExpenseRequest):
    members = [_Member(m) for m in MEMBERS]
    allocations, shared_total, personal, total = _compute_split(request.items, members)
    share_each = round(shared_total / len(members), 2)
    desc = request.ticket_name.strip() or "Ticket"

    tx_id = STATE["next_id"]
    STATE["next_id"] += 1
    payer = next(m for m in MEMBERS if m["uuid"] == request.payer_uuid)
    STATE["transactions"].append({
        "id": tx_id, "description": desc, "amount": total, "currency": "EUR",
        "payer": payer["name"], "payer_uuid": payer["uuid"], "date": "2026-03-12",
        "category": "GROCERIES",
        "allocations": [{"member": m.display_name, "amount": amt} for m, amt in allocations],
        "attachments": [],
    })

    return {"created": [{
        "description": desc, "amount": total, "id": tx_id,
        "shared_total": round(shared_total, 2),
        "detail": [
            {
                "member": m.display_name,
                "personal": round(personal.get(m.uuid, 0.0), 2),
                "shared_part": share_each,
                "total_owed": amt,
            }
            for m, amt in allocations
        ],
    }]}


class UpdateTransactionRequest(BaseModel):
    tricount_token: str
    category: Optional[str] = None
    description: Optional[str] = None


@app.patch("/api/tricount/transaction/{tx_id}")
async def update_tx(tx_id: int, request: UpdateTransactionRequest):
    for tx in STATE["transactions"]:
        if tx["id"] == tx_id:
            if request.description:
                tx["description"] = request.description
            if request.category:
                tx["category"] = request.category
    return {"ok": True}


@app.post("/api/demo/reset")
async def reset():
    STATE["transactions"] = deepcopy(SEED_TX)
    STATE["next_id"] = 4
    return {"ok": True}


app.mount("/", StaticFiles(directory=str(ROOT / "frontend"), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    print("\n  🧾  Tricount Scanner — DÉMO (données fictives, aucun appel réseau)")
    print("      http://localhost:8010\n")
    uvicorn.run(app, host="127.0.0.1", port=8010, log_level="warning")
