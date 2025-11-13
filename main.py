import os
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from bson import ObjectId
from datetime import datetime, timezone

from database import db, create_document, get_documents

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------
# Helpers
# ----------------------

def oid(id_str: str) -> ObjectId:
    try:
        return ObjectId(id_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")


def serialize(doc: Dict[str, Any]) -> Dict[str, Any]:
    if not doc:
        return doc
    d = dict(doc)
    if "_id" in d:
        d["id"] = str(d.pop("_id"))
    # Convert datetimes to isoformat
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.astimezone(timezone.utc).isoformat()
    return d


# Basic keyword-based urgency and topic detection
URGENT_KEYWORDS = {
    "loan": ["loan", "disburse", "disbursal", "approval", "approved", "when will i get", "payout"],
    "account": ["account", "update", "profile", "change", "password"],
    "kyc": ["kyc", "verify", "verification", "id", "identity"],
    "payment": ["payment", "repay", "repayment", "due", "overdue"],
}

EXTRA_URGENT = ["urgent", "asap", "immediately", "now", "help"]


def score_urgency(text: str) -> (int, Optional[str]):
    t = text.lower()
    score = 0
    topic = None

    for tp, kws in URGENT_KEYWORDS.items():
        hits = sum(1 for kw in kws if kw in t)
        if hits:
            score += 30 + 10 * (hits - 1)
            topic = tp if topic is None else topic
    if any(word in t for word in EXTRA_URGENT):
        score += 20
    # Heuristic for question about when
    if "when" in t and ("loan" in t or "disbur" in t or "approved" in t):
        score += 20
    return min(score, 100), topic


# ----------------------
# Schemas
# ----------------------
class CreateCustomer(BaseModel):
    name: str
    email: str
    phone: Optional[str] = None
    account_id: Optional[str] = None
    is_vip: bool = False
    last_loan_status: Optional[str] = None
    kyc_status: Optional[str] = None
    notes: Optional[str] = None


class CreateMessage(BaseModel):
    customer_id: str
    text: str
    channel: str = "web"
    direction: str = "inbound"  # inbound or outbound


class CSVImport(BaseModel):
    csv_text: str  # Expect headers: name,email,phone,text
    channel: str = "web"


class CreateCanned(BaseModel):
    title: str
    text: str
    tags: Optional[List[str]] = None


# ----------------------
# WebSocket manager for real-time updates
# ----------------------
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active:
            self.active.remove(websocket)

    async def broadcast(self, message: Dict[str, Any]):
        payload = {"type": "message_created", "data": message}
        for ws in list(self.active):
            try:
                await ws.send_json(payload)
            except Exception:
                self.disconnect(ws)


manager = ConnectionManager()


# ----------------------
# Core endpoints
# ----------------------
@app.get("/")
def read_root():
    return {"message": "Messaging backend running"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                response["collections"] = db.list_collection_names()[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️ Connected but Error: {str(e)[:50]}"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"
    return response


# Customers
@app.post("/api/customers")
def create_customer(payload: CreateCustomer):
    cid = create_document("customer", payload.model_dump())
    doc = db["customer"].find_one({"_id": ObjectId(cid)})
    return serialize(doc)


@app.get("/api/customers")
def list_customers(q: Optional[str] = Query(None, description="Search by name/email/phone"), limit: int = 100):
    flt: Dict[str, Any] = {}
    if q:
        flt = {"$or": [
            {"name": {"$regex": q, "$options": "i"}},
            {"email": {"$regex": q, "$options": "i"}},
            {"phone": {"$regex": q, "$options": "i"}},
        ]}
    items = [serialize(x) for x in db["customer"].find(flt).limit(limit).sort("_id", -1)]
    return {"items": items}


@app.get("/api/customers/{customer_id}")
def get_customer(customer_id: str):
    doc = db["customer"].find_one({"_id": oid(customer_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Customer not found")
    return serialize(doc)


# Messages
@app.post("/api/messages")
async def create_message(payload: CreateMessage):
    # Compute urgency/topic for inbound only
    urgency = 0
    topic = None
    if payload.direction == "inbound":
        urgency, topic = score_urgency(payload.text)
    data = payload.model_dump()
    data.update({
        "status": "open" if payload.direction == "inbound" else "sent",
        "urgency_score": urgency,
        "topic": topic,
    })
    mid = create_document("message", data)
    doc = db["message"].find_one({"_id": ObjectId(mid)})
    serialized = serialize(doc)
    # Notify listeners
    await manager.broadcast(serialized)
    return serialized


@app.get("/api/messages")
def list_messages(
    customer_id: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    topic: Optional[str] = None,
    sort: str = "-urgency",
    limit: int = 200,
):
    flt: Dict[str, Any] = {}
    if customer_id:
        flt["customer_id"] = customer_id
    if status:
        flt["status"] = status
    if topic:
        flt["topic"] = topic
    if q:
        flt["$or"] = [
            {"text": {"$regex": q, "$options": "i"}},
        ]
    cursor = db["message"].find(flt)
    if sort == "-urgency":
        cursor = cursor.sort([("urgency_score", -1), ("_id", -1)])
    else:
        cursor = cursor.sort("_id", -1)
    cursor = cursor.limit(limit)
    items = [serialize(x) for x in cursor]
    return {"items": items}


@app.get("/api/messages/{message_id}")
def get_message(message_id: str):
    doc = db["message"].find_one({"_id": oid(message_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Message not found")
    return serialize(doc)


@app.post("/api/messages/import_csv")
def import_csv(payload: CSVImport):
    import csv
    from io import StringIO

    f = StringIO(payload.csv_text)
    reader = csv.DictReader(f)
    imported = 0
    for row in reader:
        name = row.get("name") or row.get("Name")
        email = row.get("email") or row.get("Email")
        phone = row.get("phone") or row.get("Phone")
        text = row.get("text") or row.get("message") or row.get("Message")
        if not text:
            continue
        # Upsert customer by email or phone
        cust = None
        if email:
            cust = db["customer"].find_one({"email": email})
        if not cust and phone:
            cust = db["customer"].find_one({"phone": phone})
        if not cust:
            cid = create_document("customer", {
                "name": name or (email or phone or "Unknown"),
                "email": email or f"unknown+{ObjectId()}@example.com",
                "phone": phone,
            })
            cust = db["customer"].find_one({"_id": ObjectId(cid)})
        urgency, topic = score_urgency(text)
        create_document("message", {
            "customer_id": str(cust["_id"]),
            "text": text,
            "channel": payload.channel,
            "direction": "inbound",
            "status": "open",
            "urgency_score": urgency,
            "topic": topic,
        })
        imported += 1
    return {"imported": imported}


# Canned responses
@app.get("/api/canned")
def list_canned():
    items = [serialize(x) for x in db["cannedmessage"].find({}).sort("title", 1)]
    # Seed some defaults if empty
    if not items:
        defaults = [
            {"title": "Loan Disbursement Timeline", "text": "Thanks for reaching out! Once your loan is approved, disbursement typically happens within 24-48 hours. We'll notify you as soon as it's sent.", "tags": ["loan", "timeline"]},
            {"title": "KYC Verification Steps", "text": "To complete KYC, please upload a clear photo of your ID and a selfie in the app. Verification usually takes under 15 minutes.", "tags": ["kyc"]},
            {"title": "Update Account Info", "text": "You can update your phone, email, and address in the Profile section. Let me know if you'd like me to guide you step-by-step.", "tags": ["account"]},
            {"title": "Repayment Options", "text": "You can repay via the app using your preferred method. If you're having trouble, I can share a quick walkthrough.", "tags": ["payment"]},
        ]
        for d in defaults:
            create_document("cannedmessage", d)
        items = [serialize(x) for x in db["cannedmessage"].find({}).sort("title", 1)]
    return {"items": items}


@app.post("/api/canned")
def create_canned(payload: CreateCanned):
    cid = create_document("cannedmessage", payload.model_dump())
    doc = db["cannedmessage"].find_one({"_id": ObjectId(cid)})
    return serialize(doc)


# Conversations per customer
@app.get("/api/conversations")
def conversations(q: Optional[str] = None, sort: str = "-urgency", limit: int = 100):
    # Aggregate last message and max urgency per customer
    pipeline = []
    if q:
        pipeline.append({
            "$match": {"$or": [
                {"text": {"$regex": q, "$options": "i"}},
            ]}
        })
    pipeline += [
        {"$group": {
            "_id": "$customer_id",
            "last_message": {"$last": "$text"},
            "last_time": {"$last": "$_id"},
            "max_urgency": {"$max": "$urgency_score"},
            "topics": {"$addToSet": "$topic"},
        }},
        {"$sort": {("max_urgency" if sort == "-urgency" else "last_time"): -1}},
        {"$limit": limit}
    ]
    items = list(db["message"].aggregate(pipeline))
    out = []
    for it in items:
        cust = db["customer"].find_one({"_id": ObjectId(it["_id"])}) if it.get("_id") else None
        out.append({
            "customer_id": it.get("_id"),
            "customer": serialize(cust) if cust else None,
            "last_message": it.get("last_message"),
            "max_urgency": it.get("max_urgency", 0),
            "topics": [t for t in (it.get("topics") or []) if t],
        })
    return {"items": out}


# WebSocket for real-time new messages
@app.websocket("/ws/messages")
async def ws_messages(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive; clients may send ping
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
