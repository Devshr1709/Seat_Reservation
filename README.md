# Seat Reservation at Scale

A FastAPI service built on Postgres is designed for hot-seat concurrency, all-or-nothing multi-seat reservations, idempotent retries, and fail-closed readiness checks.

## What this service guarantees
- No double-sell: one seat can be confirmed to at most one user.
- All-or-nothing multi-seat requests: a request either reserves every requested seat or returns a clean domain decline.
- Idempotency: the same user + show + key resolves to exactly one reservation.
- Owner-only cancellation: a reservation can be cancelled only by the token's user.
- DB readiness is enforced: `/readyz` returns 503 while Postgres is unavailable.
- Money is stored and returned as integer paise, never floats.

## Run locally

```bash
docker compose up --build
```

This starts:
- Postgres on the internal Docker network
- the API on `http://localhost:8000`
- `ADMIN_TOKEN=admin-secret`

If you want to exercise it without Docker:

```bash
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/seats"
export ADMIN_TOKEN="admin-secret"
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Integration tests

With the project dependencies installed, start the local database and run the
Postgres-backed integrity tests:

```bash
docker compose up -d db
python3 -m unittest discover -s tests -v
```

## Health and readiness

The app exposes:
- `GET /healthz` -> liveness; returns `200` when the process is up
- `GET /readyz` -> dependency readiness; verifies the database is reachable and returns `503` when it is not

The runtime behaviour is intentionally fail-closed: if the DB is down, the service stays up but does not advertise readiness.

## Auth model

Authentication is `Authorization: Bearer <token>`.

- `ADMIN_TOKEN` is the only admin token.
- For non-admin tokens, the token string is treated as the user id.
- Request body fields such as `user_id` are ignored; identity always follows the token.

## API contract

### Create a show
`POST /shows` (admin only)

Request:

```json
{
  "name": "friday-night",
  "seats": ["A1", "A2", "A3", "A4"],
  "price_paise": 25000,
  "per_user_limit": 4
}
```

Response `201`:

```json
{
  "id": "<uuid>",
  "name": "friday-night",
  "price_paise": 25000,
  "per_user_limit": 4,
  "total_seats": 4,
  "seats": {
    "A1": "available",
    "A2": "available",
    "A3": "available",
    "A4": "available"
  }
}
```

### Reserve seats
`POST /shows/{id}/reserve`

Request body:

```json
{
  "seats": ["A12"],
  "idempotency_key": "abc123"
}
```

The same key may also be sent as the `Idempotency-Key` header. The API accepts either form.

On success, `201` returns a reservation object such as:

```json
{
  "reservation_id": "<uuid>",
  "show_id": "<uuid>",
  "user_id": "alice",
  "seats": ["A12"],
  "amount_paise": 25000,
  "status": "confirmed"
}
```

Expected outcomes:
- `201` new reservation
- `200` replay of a previously accepted request with the same key
- `409` domain decline such as `seat_taken`, `per_user_limit`, or `idempotency_key_conflict`
- `404` unknown show or unknown seat
- `401` invalid or missing bearer token

This service uses an all-or-nothing reservation model: if the request includes multiple seats and any seat is not available, the whole request fails cleanly instead of partially reserving some seats.

### Cancel a reservation
`POST /reservations/{id}/cancel`

- Only the owner of the reservation may cancel it.
- Non-owner requests return `403`.
- Duplicate cancel requests are idempotent and return the current reservation state.

Reservations are confirmed immediately. Timed holds and automatic expiry are
not implemented; owner cancellation is the release mechanism. The `held` count
is included for state compatibility but remains zero in this implementation.

### Show state
`GET /shows/{id}`

Returns:
- counts for `available`, `held`, and `confirmed`
- `total_seats`
- `reconciled` flag
- optional seat map if `include_seats` is not disabled

The invariant is:

```text
available + held + confirmed == total_seats
```

## End-to-end API example

Use either the local service or the Render deployment. The requests and
responses are the same; only `BASE_URL` and the admin token differ.

For local Docker, start the stack and set:

```bash
docker compose up --build -d
export BASE_URL="http://localhost:8000"
export ADMIN_TOKEN="admin-secret"
```

For Render, set the base URL and read the admin token privately from the
Render service's Environment page:

```bash
export BASE_URL="https://seat-reservation-b6qg.onrender.com"
read -s ADMIN_TOKEN
export ADMIN_TOKEN
```

Create a show (admin token required):

```bash
curl -i -X POST "$BASE_URL/shows" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"demo-show","seats":["A1","A2","A3"],"price_paise":25000}'
```

Expected response (`201`; use the returned ID below):

```json
{
  "id": "11111111-2222-4333-8444-555555555555",
  "name": "demo-show",
  "price_paise": 25000,
  "per_user_limit": 4,
  "total_seats": 3,
  "seats": {"A1": "available", "A2": "available", "A3": "available"}
}
```

Set the ID from that response, then let Alice reserve `A1`:

```bash
export SHOW_ID="11111111-2222-4333-8444-555555555555"
curl -i -X POST "$BASE_URL/shows/$SHOW_ID/reserve" \
  -H "Authorization: Bearer alice" \
  -H "Idempotency-Key: alice-a1-001" \
  -H "Content-Type: application/json" \
  -d '{"seats":["A1"]}'
```

Expected response (`201`):

```json
{
  "reservation_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
  "show_id": "11111111-2222-4333-8444-555555555555",
  "user_id": "alice",
  "seats": ["A1"],
  "amount_paise": 25000,
  "status": "confirmed"
}
```

Repeating Alice's request with the same key and body returns `200` and the same
reservation:

```bash
curl -i -X POST "$BASE_URL/shows/$SHOW_ID/reserve" \
  -H "Authorization: Bearer alice" \
  -H "Idempotency-Key: alice-a1-001" \
  -H "Content-Type: application/json" \
  -d '{"seats":["A1"]}'
```

Expected response: `200 OK`, header `Idempotent-Replayed: true`, and the same
`reservation_id` as Alice's first response.

Bob trying to take the same seat on the same show gets `409`:

```bash
curl -i -X POST "$BASE_URL/shows/$SHOW_ID/reserve" \
  -H "Authorization: Bearer bob" \
  -H "Idempotency-Key: bob-a1-001" \
  -H "Content-Type: application/json" \
  -d '{"seats":["A1"]}'
```

```json
{"error":"seat_taken","seats":["A1"]}
```

Check the show state; `A1` is confirmed while `A2` and `A3` remain available:

```bash
curl -sS "$BASE_URL/shows/$SHOW_ID"
```

```json
{
  "id": "11111111-2222-4333-8444-555555555555",
  "name": "demo-show",
  "price_paise": 25000,
  "per_user_limit": 4,
  "total_seats": 3,
  "available": 2,
  "held": 0,
  "confirmed": 1,
  "reconciled": true,
  "seats": {"A1":"confirmed", "A2":"available", "A3":"available"}
}
```

Alice can cancel her reservation; then Bob can rebook the released seat:

```bash
export RESERVATION_ID="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
curl -i -X POST "$BASE_URL/reservations/$RESERVATION_ID/cancel" \
  -H "Authorization: Bearer alice"
```

Cancellation returns `200` with the reservation's status set to `cancelled`.
For example:

```json
{
  "reservation_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
  "show_id": "11111111-2222-4333-8444-555555555555",
  "user_id": "alice",
  "seats": ["A1"],
  "amount_paise": 25000,
  "status": "cancelled"
}
```

Retry Bob's reserve command above; it now returns `201` with `user_id` `bob`
and a new reservation ID:

```json
{
  "reservation_id": "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
  "show_id": "11111111-2222-4333-8444-555555555555",
  "user_id": "bob",
  "seats": ["A1"],
  "amount_paise": 25000,
  "status": "confirmed"
}
```

The admin token is only for creating shows; user bearer tokens identify the
reservation owner. Never put the Render admin token in the README or source.

## Concurrency and correctness model

The atomic decision is intentionally pushed into a single transaction in Postgres:

- each seat is stored as a row in `seats(show_id, seat_id)` with a primary key `(show_id, seat_id)`
- the request is guarded by a transaction-scoped advisory lock per `(user, show)`
- multi-seat operations lock rows in sorted order with `FOR UPDATE NOWAIT`; lock contention becomes a fast `409 seat_taken` instead of occupying a DB connection while waiting
- the update is conditional on `status = 'available'`
- the transaction is retried on deadlock/serialization errors

This makes the decision race-free under concurrent reserve traffic.

## Idempotency

The idempotency table stores `(user_id, show_id, key) -> (request_hash, reservation_id)` in the same transaction as the reservation write. That means:
- a retry with the same key and same seat set replays the original reservation
- a retry with the same key and different seat set is rejected with `409`
- two concurrent requests with the same key cannot both win because they are serialized by the advisory lock

## Metrics and logging

The API exposes Prometheus metrics at `GET /metrics`.

The key metrics are:
- `reservations_confirmed_total`
- `reservations_declined_total{reason="..."}`
- `seats_available{show_id="..."}`
- HTTP counters from the request middleware

Structured logs are written to stdout with a `request_id`, and the service honors an inbound `X-Request-ID` header when present.

For a local Docker run:

```bash
curl -sS http://localhost:8000/metrics
docker compose logs -f api
```

Live Render endpoints:
- [Service root](https://seat-reservation-b6qg.onrender.com/)
- [Swagger UI](https://seat-reservation-b6qg.onrender.com/docs)
- [OpenAPI schema](https://seat-reservation-b6qg.onrender.com/openapi.json)
- [Prometheus metrics](https://seat-reservation-b6qg.onrender.com/metrics)
- [Liveness](https://seat-reservation-b6qg.onrender.com/healthz)
- [Readiness](https://seat-reservation-b6qg.onrender.com/readyz)

Render application logs are available in the service dashboard under **Logs**.
The service root and `/metrics` are currently responding. Prometheus counters
such as `reservations_confirmed_total` are process-local and reset when Render
restarts the service; the `seats_available` and `show_seats_confirmed` gauges
are read from Postgres and persist across restarts. Compare those gauges with
`GET /shows/{id}` for the same show. A decline-reason series appears after that reason occurs in the current process.

## Burst script

### Long-form recording

[Recording of the load test](https://drive.google.com/file/d/11E5fDczKvTkjauXdB4qRkDYj2GibhsK6/view?usp=sharing)

This repository includes a one-command stress tool:

```bash
make burst
```

or:

```bash
./burst.sh http://localhost:8000 --admin-token admin-secret
```

and the script supports flags such as:

```bash
./burst.sh http://localhost:8000 --admin-token admin-secret --requests 20000 --concurrency 500
```

The script prints:
- outcome distribution
- hot-seat winner count
- per-user limit behavior
- idempotency behavior
- concurrent cancel/rebook race
- final reconciliation results
- PASS/FAIL summary

### Local Docker run

After `docker compose up --build -d`, run the full default workload against
localhost:

```bash
./burst.sh http://localhost:8000 --admin-token admin-secret
```

Verified locally with the default 20,000 requests and concurrency 500:

```text
hot-seat storm S1: winners=1 (want 1)
stampede done in 61.8s; seats double-confirmed: 0 (want 0)
per-user limit: 10 parallel -> 4 confirmed (want <=4)
idempotency/spoof: 201,200,409 user=alice -> ok
cancel by non-owner=403 (want 403), by owner=200 (want 200)
cancel/rebook race: cancel=200, first_rebook=409, final_rebook=201 -> ok
reconcile: available=63 held=0 confirmed=1937 total=2000 sum=2000
201-but-not-confirmed (must be 0): 0 []
confirmed-without-201: 0 (explained by lost responses: 0, unexplained: 0 [])
RESULT: PASS
```

The run above sent 20,000 stampede requests with up to 500 in flight; it does
not mean all 20,000 were simultaneous. The burst script now also checks the
500-request hot-seat phase separately and fails unless it gets exactly one
`201`, 499 `409 seat_taken` responses, and no 5xx or client errors. The output
above is from the earlier run, before those extra hot-seat counts were printed.

### Render deployment runs

Set the admin token in your shell rather than putting a live token in command
history or this README. The script reads `ADMIN_TOKEN` from the environment:

```bash
read -s ADMIN_TOKEN
export ADMIN_TOKEN
```

On the deployed Render service, 2,000 requests at concurrency 30 completed:

```bash
./burst.sh https://seat-reservation-b6qg.onrender.com \
  --requests 2000 \
  --concurrency 30
```

```text
hot-seat storm S1: winners=1 (want 1)
stampede done in 55.2s; seats double-confirmed: 0 (want 0)
per-user limit: 10 parallel -> 4 confirmed (want <=4)
idempotency/spoof: 201,200,409 user=alice -> ok
cancel by non-owner=403 (want 403), by owner=200 (want 200)
cancel/rebook race: cancel=200, first_rebook=201, final_rebook=201 -> ok
reconcile: available=1236 held=0 confirmed=764 total=2000 sum=2000
201-but-not-confirmed (must be 0): 0 []
confirmed-without-201: 0 (explained by lost responses: 0, unexplained: 0 [])
RESULT: PASS
```

At 20,000 stampede requests and concurrency 30, the same Render free-tier deployment
preserved the seat invariants but the run returned client timeouts:

```bash
./burst.sh https://seat-reservation-b6qg.onrender.com \
  --requests 20000 \
  --concurrency 30
```

```text
stampede done in 588.7s; seats double-confirmed: 0 (want 0)
per-user limit: 10 parallel -> 4 confirmed (want <=4)
idempotency/spoof: 201,200,409 user=alice -> ok
cancel by non-owner=403 (want 403), by owner=200 (want 200)
cancel/rebook race: cancel=200, first_rebook=201, final_rebook=201 -> ok
  client_error:ReadTimeout           28
reconcile: available=60 held=0 confirmed=1940 total=2000 sum=2000
201-but-not-confirmed (must be 0): 0 []
confirmed-without-201: 0 (explained by lost responses: 0, unexplained: 0 [])
RESULT: FAIL 28 5xx/client errors
```

The burst client uses a 120-second request timeout. The 20,000-request run
therefore failed its strict client-error check because 28 responses arrived
too late or were not received. The final database reconciliation still had no
double-sold seats and no unexplained confirmations. This is the observed
capacity difference for these runs: localhost passed at the default workload,
while the Render free-tier service passed 2,000 requests at concurrency 30 and
timed out on some requests at 20,000. These results describe the tested runs;
they are not a general capacity guarantee.


## Deployment notes

The repo includes a Render-ready configuration in [render.yaml](render.yaml). It points to Docker and health-checks `/readyz`.

The service is deployed at [seat-reservation-b6qg.onrender.com](https://seat-reservation-b6qg.onrender.com/).
Use that base URL as `BASE_URL` when running the burst script. Confirm Render
has deployed the latest commit before testing; a service restart alone does
not necessarily build newly pushed code.

## Verified in this workspace

I verified the service with fresh commands:

```bash
docker compose up --build -d
curl -sS -i http://localhost:8000/readyz
curl -sS -i http://localhost:8000/healthz
python3 burst.py http://localhost:8000 --admin-token admin-secret --requests 2000 --concurrency 200 --hot 20 --seats 200
```

This returned `200 OK` on `/healthz`, `200 OK` on `/readyz`, and the burst script finished with `RESULT: PASS`.
