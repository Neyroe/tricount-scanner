import os
import asyncio
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import tricount as tc_module
from dotenv import load_dotenv

import receipt_parser

load_dotenv()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CREDENTIALS_PATH = Path("credentials.json")
TRICOUNT_URL = os.getenv("tricount_url", "")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_token(url: str) -> str:
    url = url.strip()
    if "token=" in url:
        return url.split("token=")[-1].split("&")[0].split("#")[0]
    return url.rstrip("/").split("/")[-1]


def _build_client() -> tc_module.TricountAPI:
    if CREDENTIALS_PATH.exists():
        creds = tc_module.Credentials.load(CREDENTIALS_PATH)
    else:
        creds = tc_module.Credentials.generate()
        creds.save(CREDENTIALS_PATH)
    client = tc_module.TricountAPI(creds)
    client.authenticate()
    return client


TRICOUNT_API_BASE = "https://api.tricount.bunq.com"


def _compute_split(items, active_members):
    """Compute custom-split allocations from itemized lines.

    Each member owes: their personal items + equal share of the shared items.
    Returns (allocations, shared_total, personal, total_ticket).
    allocations is a list of (member, amount_owed) with the last member
    absorbing any rounding difference so the sum matches the total exactly.
    """
    n = len(active_members)
    shared_total = 0.0
    personal: dict[str, float] = {}

    for item in items:
        if item.assigned_to == "shared":
            shared_total += item.price
        else:
            personal[item.assigned_to] = personal.get(item.assigned_to, 0.0) + item.price

    share_each = shared_total / n if n else 0.0
    total_ticket = round(sum(item.price for item in items), 2)

    allocations: list[tuple] = []
    alloc_sum = 0.0
    for i, member in enumerate(active_members):
        owed = personal.get(member.uuid, 0.0) + share_each
        if i == n - 1:
            owed = round(total_ticket - alloc_sum, 2)
        else:
            owed = round(owed, 2)
        alloc_sum += owed
        allocations.append((member, owed))

    return allocations, round(shared_total, 2), personal, total_ticket


def _fetch_attachments_by_tx(client, tricount) -> dict[int, list[dict]]:
    """Read the raw registry and map transaction id -> list of attachment dicts.

    Attachments on a Tricount transaction live in the raw registry entry's
    'attachment' field (distinct from gallery attachments).
    """
    resp = client.session.get(
        f"{TRICOUNT_API_BASE}/v1/user/{client.user_id}/registry",
        params={"public_identifier_token": tricount.public_identifier_token},
    )
    resp.raise_for_status()
    data = resp.json()

    result: dict[int, list[dict]] = {}
    for entry in data["Response"][0]["Registry"].get("all_registry_entry", []):
        re = entry.get("RegistryEntry", {})
        tx_id = re.get("id")
        atts = re.get("attachment", [])
        if not tx_id or not atts:
            continue
        out = []
        for a in atts:
            urls = a.get("urls", [])
            original = next((u["url"] for u in urls if u.get("type") == "ORIGINAL"), None)
            url = original or (urls[0]["url"] if urls else None)
            if url:
                out.append({
                    "id": a.get("id"),
                    "content_type": a.get("content_type", "image/jpeg"),
                    "url": url,
                })
        if out:
            result[tx_id] = out
    return result


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ConnectRequest(BaseModel):
    share_url: str


class UpdateTransactionRequest(BaseModel):
    tricount_token: str
    category: Optional[str] = None
    description: Optional[str] = None


class ItemAssignment(BaseModel):
    name: str
    price: float
    assigned_to: str  # "shared" or a member UUID


class CreateExpenseRequest(BaseModel):
    tricount_token: str
    payer_uuid: str
    ticket_name: str
    items: list[ItemAssignment]


class ResplitRequest(BaseModel):
    payer_uuid: Optional[str] = None
    description: Optional[str] = None
    items: list[ItemAssignment]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/config")
async def get_config():
    """Return pre-configured tricount info from .env, or empty if not set."""
    if not TRICOUNT_URL:
        return {"preconfigured": False}
    try:
        token = _extract_token(TRICOUNT_URL)
        def _sync():
            client = _build_client()
            t = client.join_tricount(token)
            return {
                "preconfigured": True,
                "token": token,
                "name": t.title,
                "currency": t.currency,
                "members": [
                    {"uuid": m.uuid, "name": m.display_name}
                    for m in t.members if m.status == "ACTIVE"
                ],
            }
        return await asyncio.to_thread(_sync)
    except Exception as e:
        return {"preconfigured": False, "error": str(e)}


@app.post("/api/connect")
async def connect(request: ConnectRequest):
    token = _extract_token(request.share_url)

    def _sync():
        client = _build_client()
        t = client.join_tricount(token)
        return {
            "token": token,
            "name": t.title,
            "currency": t.currency,
            "members": [
                {"uuid": m.uuid, "name": m.display_name}
                for m in t.members
                if m.status == "ACTIVE"
            ],
        }

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/parse-csv")
async def parse_receipt_endpoint(file: UploadFile = File(...)):
    """Parse a receipt export — CSV or PDF (E.Leclerc, générique)."""
    raw = await file.read()
    try:
        result = receipt_parser.parse_receipt(raw, file.filename or "")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not result.get("items"):
        raise HTTPException(
            status_code=422,
            detail="Aucun article trouvé. Vérifiez le format du fichier.",
        )
    return result


@app.post("/api/create-expense")
async def create_expense(request: CreateExpenseRequest):
    def _sync():
        client = _build_client()
        t = client.join_tricount(request.tricount_token)

        member_by_uuid = {m.uuid: m for m in t.members}
        payer = member_by_uuid.get(request.payer_uuid)
        if not payer:
            raise ValueError("Payeur introuvable dans le tricount")

        active_members = [m for m in t.members if m.status == "ACTIVE"]

        allocations, shared_total, personal, total_ticket = _compute_split(
            request.items, active_members
        )
        share_each = round(shared_total / len(active_members), 2) if active_members else 0.0
        desc = request.ticket_name.strip() or "Ticket"

        tx_id = client.create_transaction_custom_split(
            tricount=t,
            description=desc,
            amount=total_ticket,
            payer=payer,
            allocations=allocations,
            category=tc_module.Category.GROCERIES,
        )

        detail = [
            {
                "member": m.display_name,
                "personal": round(personal.get(m.uuid, 0.0), 2),
                "shared_part": share_each,
                "total_owed": amt,
            }
            for m, amt in allocations
        ]

        return {
            "created": [{
                "description": desc,
                "amount": total_ticket,
                "id": tx_id,
                "shared_total": round(shared_total, 2),
                "detail": detail,
            }]
        }

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/tricount/overview")
async def tricount_overview():
    """Return recent transactions and computed balances for the configured tricount."""
    if not TRICOUNT_URL:
        raise HTTPException(status_code=400, detail="tricount_url non configuré dans .env")

    def _sync():
        token = _extract_token(TRICOUNT_URL)
        client = _build_client()
        t = client.join_tricount(token)

        member_by_uuid = {m.uuid: m for m in t.members}

        # Compute net balances: positive = member is owed money, negative = member owes
        balances: dict[str, float] = {m.uuid: 0.0 for m in t.members}

        active_tx = [tx for tx in t.transactions if tx.status.value == "ACTIVE"]
        for tx in active_tx:
            total = abs(tx.amount.as_float)
            payer_uuid = tx.membership_uuid_owner
            if payer_uuid in balances:
                balances[payer_uuid] += total
            for alloc in tx.allocations:
                uuid = alloc.membership_uuid
                if uuid in balances:
                    balances[uuid] -= abs(alloc.amount.as_float)

        members_out = [
            {
                "uuid": m.uuid,
                "name": m.display_name,
                "balance": round(balances.get(m.uuid, 0.0), 2),
            }
            for m in t.members
            if m.status == "ACTIVE"
        ]

        # Last 20 transactions, most recent first
        def _parse_date(tx):
            try:
                return tx.date or ""
            except Exception:
                return ""

        sorted_tx = sorted(active_tx, key=_parse_date, reverse=True)[:20]

        # Map transaction id -> attachments (receipt photos)
        try:
            atts_by_tx = _fetch_attachments_by_tx(client, t)
        except Exception:
            atts_by_tx = {}

        transactions_out = []
        for tx in sorted_tx:
            payer = member_by_uuid.get(tx.membership_uuid_owner)
            allocations_out = []
            for alloc in tx.allocations:
                m = member_by_uuid.get(alloc.membership_uuid)
                allocations_out.append({
                    "member": m.display_name if m else "?",
                    "amount": abs(alloc.amount.as_float),
                })
            transactions_out.append({
                "id": tx.id,
                "description": tx.description,
                "amount": abs(tx.amount.as_float),
                "currency": tx.amount.currency,
                "payer": payer.display_name if payer else "?",
                "payer_uuid": tx.membership_uuid_owner,
                "date": tx.date[:10] if tx.date else "",
                "category": tx.category,
                "allocations": allocations_out,
                "attachments": atts_by_tx.get(tx.id, []),
            })

        return {
            "name": t.title,
            "currency": t.currency,
            "members": members_out,
            "transactions": transactions_out,
        }

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/tricount/gallery")
async def list_gallery():
    """List gallery attachments (receipt photos) uploaded to the Tricount."""
    if not TRICOUNT_URL:
        raise HTTPException(status_code=400, detail="tricount_url non configuré dans .env")

    def _sync():
        token = _extract_token(TRICOUNT_URL)
        client = _build_client()
        t = client.join_tricount(token)
        member_by_uuid = {m.uuid: m for m in t.members}
        attachments = client.list_gallery_attachments(t)
        result = []
        for a in attachments:
            uploader = member_by_uuid.get(a.membership_uuid)
            thumbnail = next((u.url for u in a.urls if u.url_type != "ORIGINAL"), None)
            result.append({
                "id": a.id,
                "uuid": a.uuid,
                "content_type": a.content_type,
                "url": a.original_url,
                "thumbnail_url": thumbnail or a.original_url,
                "uploader": uploader.display_name if uploader else "?",
            })
        return {"attachments": result}

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/tricount/attachment/proxy")
async def proxy_attachment(url: str = Query(...)):
    """Proxy an attachment image through the authenticated Tricount session."""
    if not TRICOUNT_URL:
        raise HTTPException(status_code=400, detail="tricount_url non configuré")

    def _sync():
        token = _extract_token(TRICOUNT_URL)
        client = _build_client()
        client.join_tricount(token)  # ensures session is authenticated
        r = client.session.get(url, timeout=30)
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "image/jpeg")

    try:
        content, content_type = await asyncio.to_thread(_sync)
        return Response(content=content, media_type=content_type)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


class SaveAttachmentRequest(BaseModel):
    url: str
    filename: str = "ticket.jpg"


@app.post("/api/tricount/attachment/save")
async def save_attachment(request: SaveAttachmentRequest):
    """Download an attachment and save it to ~/Downloads/."""
    if not TRICOUNT_URL:
        raise HTTPException(status_code=400, detail="tricount_url non configuré")

    def _sync():
        token = _extract_token(TRICOUNT_URL)
        client = _build_client()
        client.join_tricount(token)
        r = client.session.get(request.url, timeout=30)
        r.raise_for_status()

        downloads = Path.home() / "Downloads"
        downloads.mkdir(exist_ok=True)

        # Avoid overwriting: add suffix if file exists
        dest = downloads / request.filename
        stem, suffix = Path(request.filename).stem, Path(request.filename).suffix
        counter = 1
        while dest.exists():
            dest = downloads / f"{stem}_{counter}{suffix}"
            counter += 1

        dest.write_bytes(r.content)
        return {"path": str(dest), "filename": dest.name}

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/api/tricount/transaction/{tx_id}")
async def update_transaction(tx_id: int, request: UpdateTransactionRequest):
    def _sync():
        client = _build_client()
        t = client.join_tricount(request.tricount_token)
        cat = tc_module.Category[request.category] if request.category else None
        client.edit_transaction(
            tricount=t,
            transaction_id=tx_id,
            category=cat,
            description=request.description or None,
        )
        return {"ok": True}
    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/tricount/transaction/{tx_id}/resplit")
async def resplit_transaction(tx_id: int, request: ResplitRequest):
    """Re-split an existing transaction from itemized lines, preserving its photo.

    The transaction total is recomputed as the sum of the items; each member
    owes their personal items + an equal share of the shared items.
    """
    if not TRICOUNT_URL:
        raise HTTPException(status_code=400, detail="tricount_url non configuré")

    def _sync():
        token = _extract_token(TRICOUNT_URL)
        client = _build_client()
        t = client.join_tricount(token)

        tx = next((x for x in t.transactions if x.id == tx_id), None)
        if not tx:
            raise ValueError("Transaction introuvable")

        member_by_uuid = {m.uuid: m for m in t.members}
        payer_uuid = request.payer_uuid or tx.membership_uuid_owner
        payer = member_by_uuid.get(payer_uuid)
        if not payer:
            raise ValueError("Payeur introuvable")

        active_members = [m for m in t.members if m.status == "ACTIVE"]
        allocations, shared_total, personal, total_ticket = _compute_split(
            request.items, active_members
        )
        share_each = round(shared_total / len(active_members), 2) if active_members else 0.0
        desc = (request.description or tx.description or "").strip() or "Dépense"

        # Preserve existing attachments
        atts_by_tx = _fetch_attachments_by_tx(client, t)
        attachment_ids = [{"id": a["id"]} for a in atts_by_tx.get(tx_id, []) if a.get("id")]

        # Build allocations (negative values, as Tricount stores expenses)
        alloc_list = [
            {
                "membership_uuid": m.uuid,
                "amount": {"value": str(-amt), "currency": t.currency},
                "type": "AMOUNT",
            }
            for m, amt in allocations
        ]

        payload = {
            "description": desc,
            "amount": {"value": str(-total_ticket), "currency": t.currency},
            "membership_uuid_owner": payer.uuid,
            "allocations": alloc_list,
            "type_transaction": tx.transaction_type.value,
            "status": "ACTIVE",
            "date": tx.date,
        }
        if attachment_ids:
            payload["attachment"] = attachment_ids
        if tx.category:
            payload["category"] = tx.category
        if tx.category_custom:
            payload["category_custom"] = tx.category_custom

        resp = client.session.put(
            f"{TRICOUNT_API_BASE}/v1/user/{client.user_id}/registry/{t.id}/registry-entry/{tx_id}",
            json=payload,
        )
        resp.raise_for_status()

        detail = [
            {
                "member": m.display_name,
                "personal": round(personal.get(m.uuid, 0.0), 2),
                "shared_part": share_each,
                "total_owed": amt,
            }
            for m, amt in allocations
        ]
        return {
            "ok": True,
            "id": tx_id,
            "description": desc,
            "amount": total_ticket,
            "shared_total": shared_total,
            "payer": payer.display_name,
            "detail": detail,
        }

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
