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

For a hosted deployment, open `https://<service-host>/metrics` for Prometheus
text output and use the hosting provider's service log viewer (Render:
Dashboard -> service -> Logs). No live service URL is available yet.

## Burst script

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
- final reconciliation results
- PASS/FAIL summary

## Deployment notes

The repo includes a Render-ready configuration in [render.yaml](render.yaml). It points to Docker and health-checks `/readyz`.

The expected deployment flow is:

```bash
# in a host that has Docker or a managed app runtime
# set DATABASE_URL and ADMIN_TOKEN
# deploy the repo or push to Render / Railway / Fly
```

Live URL: not deployed yet. `render.yaml` is deployment configuration; it does
not create a hosted service by itself. After deployment, use the service URL as
`BASE_URL` when running the burst script and use the provider dashboard for
runtime logs.

## Verified in this workspace

I verified the service with fresh commands:

```bash
docker compose up --build -d
curl -sS -i http://localhost:8000/readyz
curl -sS -i http://localhost:8000/healthz
python3 burst.py http://localhost:8000 --admin-token admin-secret --requests 2000 --concurrency 200 --hot 20 --seats 200
```

This returned `200 OK` on `/healthz`, `200 OK` on `/readyz`, and the burst script finished with `RESULT: PASS`.
