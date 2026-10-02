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
        hot_storm = dist.copy()
        hot_winners = hot_storm.get("201", 0)
        hot_declines = hot_storm.get("409:seat_taken", 0)
        print("hot-seat outcomes:", dict(sorted(hot_storm.items())))
        hot_errors = sum(
            count for outcome, count in hot_storm.items()
            if outcome.startswith("5") or outcome.startswith("client_error")
        )
        print(
            f"hot-seat storm S1: winners={hot_winners} (want 1), "
            f"seat_taken={hot_declines} (want 499), errors={hot_errors} (want 0)"
        )
        if hot_winners != 1:
            fails.append(f"hot seat returned {hot_winners} winners (want 1)")
        if hot_declines != 499:
            fails.append(f"hot seat returned {hot_declines} seat_taken declines (want 499)")
        if hot_winners + hot_declines != 500:
            fails.append(
                f"hot-seat storm had {hot_winners + hot_declines} expected outcomes (want 500)"
            )
        if hot_errors:
            fails.append(f"hot-seat storm had {hot_errors} 5xx/client errors")
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

        # 5. race an owner's cancellation against another user's rebooking
        sh3_response = await cl.post(
            f"{base}/shows",
            json={"name": "cancel-rebook", "seats": ["R1"], "price_paise": 100},
            headers=H(a.admin_token),
        )
        sh3_response.raise_for_status()
        sh3 = sh3_response.json()["id"]
        seed = await cl.post(
            f"{base}/shows/{sh3}/reserve",
            json={"seats": ["R1"], "idempotency_key": "race-seed"},
            headers=H("alice"),
        )
        if seed.status_code != 201:
            fails.append(f"cancel/rebook setup: reserve returned {seed.status_code}")
        else:
            race_rid = seed.json()["reservation_id"]
            race_key = uuid.uuid4().hex
            cancel_response, first_rebook = await asyncio.gather(
                cl.post(f"{base}/reservations/{race_rid}/cancel", headers=H("alice")),
                cl.post(
                    f"{base}/shows/{sh3}/reserve",
                    json={"seats": ["R1"], "idempotency_key": race_key},
                    headers=H("bob"),
                ),
            )
            rebook = first_rebook
            if first_rebook.status_code == 409:
                error = first_rebook.json().get("error")
                if error == "seat_taken":
                    rebook = await cl.post(
                        f"{base}/shows/{sh3}/reserve",
                        json={"seats": ["R1"], "idempotency_key": race_key},
                        headers=H("bob"),
                    )
            race_state = (await cl.get(f"{base}/shows/{sh3}")).json()
            race_ok = (
                cancel_response.status_code == 200
                and rebook.status_code == 201
                and rebook.json().get("user_id") == "bob"
                and race_state["confirmed"] == 1
                and race_state["available"] == 0
                and race_state["reconciled"]
            )
            print(
                "cancel/rebook race: "
                f"cancel={cancel_response.status_code}, "
                f"first_rebook={first_rebook.status_code}, "
                f"final_rebook={rebook.status_code} -> {'ok' if race_ok else 'FAIL'}"
            )
            if not race_ok:
                fails.append("concurrent cancel/rebook or final seat state")

        # 6. reconciliation: client-side 201s vs server-side truth
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
