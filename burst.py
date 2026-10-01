"""On-sale stampede. Usage: python burst.py BASE_URL [--admin-token T] [--requests 20000] [--concurrency 500]"""
import argparse, asyncio, collections, os, random, sys, time, uuid
import httpx

ap = argparse.ArgumentParser()
ap.add_argument("base"); ap.add_argument("--admin-token", default=os.environ.get("ADMIN_TOKEN", "admin-secret"))
ap.add_argument("--requests", type=int, default=20000)
ap.add_argument("--concurrency", type=int, default=500)
ap.add_argument("--seats", type=int, default=2000)
ap.add_argument("--hot", type=int, default=20)
a = ap.parse_args()
base = a.base.rstrip("/")
dist = collections.Counter()
winners = collections.defaultdict(set)   # seat -> reservation ids that got a 201
fails = []
unknown = set()   # seats in requests whose outcome the client never saw (timeout/disconnect)

def H(tok): return {"Authorization": f"Bearer {tok}"}

async def reserve(cl, sem, user, seats, key=None, extra=None):
    body = {"seats": seats, "idempotency_key": key or uuid.uuid4().hex, **(extra or {})}
    async with sem:
        try:
            r = await cl.post(f"{base}/shows/{SHOW}/reserve", json=body, headers=H(user))
        except Exception as e:
            dist["client_error:" + type(e).__name__] += 1
            unknown.update(seats)
            return None
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    label = str(r.status_code) + (":" + j["error"] if "error" in j else "")
    if r.status_code == 200: label += ":replay"
    dist[label] += 1
    if r.status_code == 201:
        for s in j["seats"]: winners[s].add(j["reservation_id"])
    return r

async def main():
    global SHOW
    seats = [f"S{i}" for i in range(1, a.seats + 1)]
    hot = seats[:a.hot]
    async with httpx.AsyncClient(timeout=120, limits=httpx.Limits(max_connections=a.concurrency)) as cl:
        r = await cl.post(f"{base}/shows", json={"name": "burst", "seats": seats, "price_paise": 25000}, headers=H(a.admin_token))
        r.raise_for_status(); SHOW = r.json()["id"]
        sem = asyncio.Semaphore(a.concurrency)
        t0 = time.time()

        # 1. hot-seat storm: 500 distinct users, same seat
        await asyncio.gather(*[reserve(cl, sem, f"storm{i}", ["S1"]) for i in range(500)])
        print(f"hot-seat storm S1: winners={len(winners['S1'])} (want 1)")
        if len(winners["S1"]) != 1: fails.append("hot seat not exactly one winner")
        dist.clear()   # keep winners: S1's winner is real and must stay in the final reconciliation

        # 2. full stampede: contended hot seats, random seats, 10% retries with same key
        jobs = []
        for i in range(a.requests):
            user = f"u{random.randint(0, 6000)}"
            pool = hot if random.random() < 0.6 else seats
            k = random.choice([1, 1, 1, 2])
            req = random.sample(pool, k)
            key = uuid.uuid4().hex
            jobs.append(reserve(cl, sem, user, req, key))
            if random.random() < 0.10: jobs.append(reserve(cl, sem, user, req, key))  # retry
        random.shuffle(jobs)
        await asyncio.gather(*jobs)
        dup = {s: v for s, v in winners.items() if len(v) > 1}
        print(f"stampede done in {time.time()-t0:.1f}s; seats double-confirmed: {len(dup)} (want 0)")
        if dup: fails.append(f"double-sold: {list(dup)[:5]}")
        five = sum(v for k, v in dist.items() if k.startswith("5") or k.startswith("client_error"))
        if five: fails.append(f"{five} 5xx/client errors")

        # 3. per-user limit under concurrency
        sh2 = (await cl.post(f"{base}/shows", json={"name": "lim", "seats": [f"L{i}" for i in range(20)], "price_paise": 100}, headers=H(a.admin_token))).json()["id"]
        rs = await asyncio.gather(*[cl.post(f"{base}/shows/{sh2}/reserve", json={"seats": [f"L{i}"], "idempotency_key": f"k{i}"}, headers=H("greedy")) for i in range(10)])
        got = sum(r.status_code == 201 for r in rs)
        print(f"per-user limit: 10 parallel -> {got} confirmed (want <=4)")
        if got > 4: fails.append("limit exceeded")

        # 4. idempotency + spoof + cancel
        k = uuid.uuid4().hex
        r1 = await cl.post(f"{base}/shows/{sh2}/reserve", json={"seats": ["L15"], "idempotency_key": k, "user_id": "victim"}, headers=H("alice"))
        r2 = await cl.post(f"{base}/shows/{sh2}/reserve", json={"seats": ["L15"], "idempotency_key": k}, headers=H("alice"))
        r3 = await cl.post(f"{base}/shows/{sh2}/reserve", json={"seats": ["L16"], "idempotency_key": k}, headers=H("alice"))
        ok = (r1.status_code == 201 and r2.status_code == 200 and r2.json()["reservation_id"] == r1.json()["reservation_id"]
              and r3.status_code == 409 and r1.json()["user_id"] == "alice")
        print(f"idempotency/spoof: {r1.status_code},{r2.status_code},{r3.status_code} user={r1.json().get('user_id')} -> {'ok' if ok else 'FAIL'}")
        if not ok: fails.append("idempotency/spoof")
        rid = r1.json()["reservation_id"]
        c1 = await cl.post(f"{base}/reservations/{rid}/cancel", headers=H("mallory"))
        c2 = await cl.post(f"{base}/reservations/{rid}/cancel", headers=H("alice"))
        print(f"cancel by non-owner={c1.status_code} (want 403), by owner={c2.status_code} (want 200)")
        if c1.status_code != 403 or c2.status_code != 200: fails.append("cancel auth")

        # 5. reconciliation: client-side 201s vs server-side truth
        s = (await cl.get(f"{base}/shows/{SHOW}")).json()
        total = s["available"] + s["held"] + s["confirmed"]
        print("\noutcome distribution:")
        for k_, v in sorted(dist.items()): print(f"  {k_:<35}{v}")
        print(f"\nreconcile: available={s['available']} held={s['held']} confirmed={s['confirmed']} total={s['total_seats']} sum={total}")
        if total != s["total_seats"]: fails.append("invariant broken")
        db_conf = {k_ for k_, v in s["seats"].items() if v == "confirmed"}
        won = set(winners)
        phantom = won - db_conf                 # client saw 201, DB says not confirmed -> REAL BUG
        extra = db_conf - won                   # confirmed, no 201 seen
        lost_ack = extra & unknown              # explained by lost responses
        unexplained = extra - unknown           # confirmed, never saw 201, no failed request touched it -> REAL BUG
        print(f"201-but-not-confirmed (must be 0): {len(phantom)} {sorted(phantom)[:10]}")
        print(f"confirmed-without-201: {len(extra)} (explained by lost responses: {len(lost_ack)}, unexplained: {len(unexplained)} {sorted(unexplained)[:10]})")
        if phantom: fails.append(f"{len(phantom)} seats got 201 but are not confirmed")
        if unexplained: fails.append(f"{len(unexplained)} confirmed seats with no matching 201 or failed request")
        m = (await cl.get(f"{base}/metrics")).text
        print("metrics:", [l for l in m.splitlines() if l.startswith(("reservations_confirmed_total", "reservations_declined_total", "seats_available{show_id=\"" + SHOW))])
    print("\nRESULT:", "PASS" if not fails else "FAIL " + "; ".join(fails))
    sys.exit(1 if fails else 0)

asyncio.run(main())
