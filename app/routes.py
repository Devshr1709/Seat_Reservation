import hashlib
import json
import time
import uuid

import asyncpg
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import db
from .auth import admin, token
from .db import ensure_db, tx
from .logging_utils import L, req_id_var
from .metrics import CANCELLED, CONFIRMED, DECLINED, HTTP, SEATS_AVAILABLE, SEATS_CONFIRMED, SEATS_CONFIRMED_G
from .models import ReserveIn, ShowIn, resv_json, valid_uuid

app = FastAPI(title="Seat Reservation")


@app.on_event("startup")
async def startup():
    try:
        await ensure_db()
    except Exception as e:  # come up anyway; /readyz fails closed until DB is reachable
        L("db_init_failed", error=str(e))


@app.middleware("http")
async def mw(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    req_id_var.set(rid)
    t0 = time.time()
    try:
        resp = await call_next(request)
    except Exception as e:
        L("unhandled", error=repr(e), path=request.url.path)
        resp = JSONResponse({"error": "internal"}, status_code=500)
    resp.headers["x-request-id"] = rid
    HTTP.labels(request.method, str(resp.status_code)).inc()
    L("request", method=request.method, path=request.url.path, status=resp.status_code, ms=round((time.time() - t0) * 1000, 1))
    return resp


@app.exception_handler(asyncpg.PostgresConnectionError)
async def _dbdown(request, exc):
    return JSONResponse({"error": "dependency_unavailable"}, status_code=503)


@app.exception_handler(db.PoolAcquireTimeout)
async def _pool_busy(request, exc):
    DECLINED.labels("server_busy").inc()
    return JSONResponse(
        {"error": "server_busy"},
        status_code=429,
        headers={"Retry-After": "1"},
    )


def err(status, code, **extra):
    return JSONResponse({"error": code, **extra}, status_code=status)


@app.get("/")
async def root():
    return {
        "service": "seat-reservation",
        "links": {
            "health": "/healthz",
            "readiness": "/readyz",
            "docs": "/docs",
            "metrics": "/metrics",
        },
    }


@app.post("/shows", status_code=201)
async def create_show(body: ShowIn, _=Depends(admin)):
    if len(set(body.seats)) != len(body.seats) or any(not s or len(s) > 32 for s in body.seats):
        return err(422, "invalid_seats", detail="seats must be unique, non-empty, <=32 chars")
    sid = uuid.uuid4()

    async def fn(c):
        await c.execute("insert into shows(id,name,price_paise,per_user_limit,total_seats) values($1,$2,$3,$4,$5)",
                        sid, body.name, body.price_paise, body.per_user_limit, len(body.seats))
        await c.execute("insert into seats(show_id,seat_id) select $1, unnest($2::text[])", sid, body.seats)

    await tx(fn)
    return {"id": str(sid), "name": body.name, "price_paise": body.price_paise,
            "per_user_limit": body.per_user_limit, "total_seats": len(body.seats),
            "seats": {s: "available" for s in body.seats}}


@app.get("/shows/{show_id}")
async def get_show(show_id: str, include_seats: bool = True):
    sid = valid_uuid(show_id)
    if not sid:
        return err(404, "show_not_found")
    async with db.acquire() as c:
        async with c.transaction(isolation="repeatable_read", readonly=True):
            s = await c.fetchrow("select * from shows where id=$1", sid)
            if not s:
                return err(404, "show_not_found")
            rows = await c.fetch("select seat_id, status from seats where show_id=$1 order by seat_id", sid)
    counts = {"available": 0, "held": 0, "confirmed": 0}
    for r in rows:
        counts[r["status"]] += 1
    out = {"id": str(sid), "name": s["name"], "price_paise": s["price_paise"],
           "per_user_limit": s["per_user_limit"], "total_seats": s["total_seats"], **counts,
           "reconciled": sum(counts.values()) == s["total_seats"]}
    if include_seats:
        out["seats"] = {r["seat_id"]: r["status"] for r in rows}
    return out


@app.post("/shows/{show_id}/reserve")
async def reserve(show_id: str, body: ReserveIn, user: str = Depends(token),
                  idempotency_key: str | None = Header(None)):
    sid = valid_uuid(show_id)
    if not sid:
        return err(404, "show_not_found")
    key = idempotency_key or body.idempotency_key
    if not key:
        return err(400, "idempotency_key_required")
    seats = body.seats
    if len(set(seats)) != len(seats):
        return err(422, "duplicate_seats")
    seats = sorted(seats)
    req_hash = hashlib.sha256(json.dumps(seats).encode()).hexdigest()

    async def fn(c):
        await c.execute("select pg_advisory_xact_lock(hashtextextended($1, 0))", f"{user}|{sid}")
        row = await c.fetchrow("select request_hash, reservation_id from idempotency "
                               "where user_id=$1 and show_id=$2 and key=$3", user, sid, key)
        if row:
            if row["request_hash"] != req_hash:
                return ("key_conflict", None)
            return ("replay", await c.fetchrow("select * from reservations where id=$1", row["reservation_id"]))
        show = await c.fetchrow("select price_paise, per_user_limit from shows where id=$1", sid)
        if not show:
            return ("show_not_found", None)
        pre = await c.fetch("select seat_id, status from seats where show_id=$1 and seat_id=any($2::text[])", sid, seats)
        if len(pre) != len(seats):
            return ("unknown_seat", sorted(set(seats) - {r["seat_id"] for r in pre}))
        taken = sorted(r["seat_id"] for r in pre if r["status"] != "available")
        if taken:
            return ("seat_taken", taken)
        held = await c.fetchval("select count(*) from seats where show_id=$1 and user_id=$2 "
                                "and status in ('held','confirmed')", sid, user)
        if held + len(seats) > show["per_user_limit"]:
            return ("per_user_limit", None)
        locked = await c.fetch("select seat_id, status from seats where show_id=$1 and seat_id=any($2::text[]) "
                               "order by seat_id for update nowait", sid, seats)
        taken = sorted(r["seat_id"] for r in locked if r["status"] != "available")
        if taken:
            return ("seat_taken", taken)
        rid = uuid.uuid4()
        n = await c.execute("update seats set status='confirmed', user_id=$3, reservation_id=$4 "
                            "where show_id=$1 and seat_id=any($2::text[]) and status='available'",
                            sid, seats, user, rid)
        assert n == f"UPDATE {len(seats)}"
        amount = show["price_paise"] * len(seats)
        r = await c.fetchrow("insert into reservations(id,show_id,user_id,seats,amount_paise,status) "
                             "values($1,$2,$3,$4,$5,'confirmed') returning *", rid, sid, user, seats, amount)
        await c.execute("insert into idempotency(user_id,show_id,key,request_hash,reservation_id) "
                        "values($1,$2,$3,$4,$5)", user, sid, key, req_hash, rid)
        return ("ok", r)

    try:
        kind, val = await tx(fn)
    except asyncpg.LockNotAvailableError:
        kind, val = "seat_taken", seats
    if kind == "ok":
        CONFIRMED.inc(); SEATS_CONFIRMED.inc(len(seats))
        L("reserved", user=user, seats=seats, show=str(sid))
        return JSONResponse(resv_json(val), status_code=201)
    if kind == "replay":
        DECLINED.labels("idempotent_replay").inc()
        return JSONResponse(resv_json(val), status_code=200, headers={"Idempotent-Replayed": "true"})
    if kind == "key_conflict":
        DECLINED.labels("idempotency_key_conflict").inc()
        return err(409, "idempotency_key_conflict")
    if kind == "seat_taken":
        DECLINED.labels("seat_taken").inc()
        return err(409, "seat_taken", seats=val)
    if kind == "per_user_limit":
        DECLINED.labels("per_user_limit").inc()
        return err(409, "per_user_limit")
    if kind == "unknown_seat":
        DECLINED.labels("unknown_seat").inc()
        return err(404, "unknown_seat", seats=val)
    return err(404, "show_not_found")


@app.post("/reservations/{rid}/cancel")
async def cancel(rid: str, user: str = Depends(token)):
    r_id = valid_uuid(rid)
    if not r_id:
        return err(404, "reservation_not_found")

    async def fn(c):
        r = await c.fetchrow("select * from reservations where id=$1 for update", r_id)
        if not r:
            return ("nf", None)
        if r["user_id"] != user:
            return ("forbidden", None)
        if r["status"] == "cancelled":
            return ("ok", r)
        await c.execute("update seats set status='available', user_id=null, reservation_id=null "
                        "where reservation_id=$1 and status='confirmed'", r_id)
        r = await c.fetchrow("update reservations set status='cancelled' where id=$1 returning *", r_id)
        return ("done", r)

    kind, r = await tx(fn)
    if kind == "nf":
        return err(404, "reservation_not_found")
    if kind == "forbidden":
        return err(403, "not_owner")
    if kind == "done":
        CANCELLED.inc()
    return resv_json(r)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    try:
        await __import__("asyncio").wait_for(ensure_db(), 5)
        await db.POOL.fetchval("select 1", timeout=2)
        return {"status": "ready"}
    except Exception as e:
        L("not_ready", error=repr(e))
        return JSONResponse({"status": "not_ready"}, status_code=503)


@app.get("/metrics")
async def metrics():
    try:
        await ensure_db()
        rows = await db.POOL.fetch("select show_id::text, count(*) filter (where status='available') a, "
                                  "count(*) filter (where status='confirmed') c from seats group by show_id", timeout=5)
        for r in rows:
            SEATS_AVAILABLE.labels(r["show_id"]).set(r["a"])
            SEATS_CONFIRMED_G.labels(r["show_id"]).set(r["c"])
    except Exception as e:
        L("metrics_db_error", error=repr(e))
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
