# Write-up

## System shape
The service uses FastAPI in front of Postgres as the system of record. The database owns the truth for seat ownership, reservation state, and idempotency. The API layer does not attempt to maintain a second source of truth or infer seat status from memory; it asks Postgres for the authoritative answer and only takes writes inside a transaction.

This approach matters because the correctness bar is framed around concurrency. A reservation request is not a read-then-write checklist in application memory; it is one atomic database transaction that decides whether the requested seats are still free and whether the user is still allowed to use them.

## Atomic decision and race-free seat allocation
The key mechanism is a Postgres transaction guarded by a per-(user, show) advisory lock and a conditional update on seat rows.

In the implementation, the reservation path does the following:
1. Serializes a user’s requests for a show with `pg_advisory_xact_lock`.
2. Looks up the idempotency record for `(user_id, show_id, key)`.
3. Validates the show and requested seats.
4. Re-checks each requested seat under a row lock.
5. Updates only seats whose current state is `available`.
6. Inserts the reservation row and idempotency record in the same transaction.

The critical bit is the guarded conditional write:

```sql
update seats
set status = 'confirmed', user_id = $3, reservation_id = $4
where show_id = $1
  and seat_id = any($2::text[])
  and status = 'available'
```

This is the atomic decision point. It is what prevents two concurrent reservations from both confirming the same seat. Under load, exactly one transaction wins the row update; the losers do not receive a 500 or a partial success — they receive a clean 409 decline and the app never guesses about state.

For multi-seat requests, the code sorts the requested seats and then locks the relevant seat rows in a consistent order. That removes lock cycles and keeps lock acquisition deterministic. The API also treats multi-seat reservation as all-or-nothing: if the request asks for more than one seat and any seat is unavailable, the transaction aborts and the whole request fails cleanly.

## Idempotency and exact-once semantics
Idempotency is enforced at the database layer, not via local app-side caching. The table is:

```sql
create table if not exists idempotency(
  user_id text not null,
  show_id uuid not null,
  key text not null,
  request_hash text not null,
  reservation_id uuid not null,
  primary key (user_id, show_id, key));
```

The request hash is computed from the sorted seat list, so:
- same user + same show + same key + same seat set => replay the original reservation
- same user + same show + same key + different seat set => `409 idempotency_key_conflict`

This is stored in the same transaction as the reservation row insertion, so there is no window where the API can create a duplicate reservation for the same key and then fail to persist the idempotency record.

## Per-user limits and ownership
The per-user limit is checked inside the same transaction that serializes the user’s requests. That guards against a race where a user fires many parallel requests that all read “I still have room” before the first one commits.

The implementation checks the currently held/confirmed count for that user and the show, and the transaction rejects the request when the user would exceed `per_user_limit`. This preserves the invariant even under concurrency.

Identity is always token-derived. The code ignores body fields like `user_id` and uses the bearer token as the user id. That prevents request-body spoofing from changing authority or ownership.

## Cancellation model
The service chooses the explicit-cancel model. Seats are confirmed immediately and released only by calling `POST /reservations/{id}/cancel` by the reservation owner. The cancellation path locks the reservation row and then updates seats where `reservation_id = $1` and `status = 'confirmed'`.

This matters because it prevents a stale cancel from “releasing” a seat that has already been sold to someone else. A reservation can only release the seats associated with that reservation, and only while those rows are still in the confirmed state.

The cancellation logic is deliberately strict:
- non-owner => `403 not_owner`
- unknown reservation => `404 reservation_not_found`
- owner cancels => reservation status transitions to `cancelled` and the seats are made available again

## Reconciliation invariant
The app reports counts from the database on `GET /shows/{id}` and computes:

```text
available + held + confirmed == total_seats
```

In this implementation, there is no timed hold state. Seats are either available or confirmed; the `held` count remains zero unless the system is later extended to add a hold model. Even then, the same reconciliation rule still applies. This is the invariant the burst script checks after load.

## Database readiness and fail-closed behavior
The service is intentionally conservative when Postgres is unavailable. The startup hook calls `ensure_db()` and logs if it fails, but the app still serves requests. That is by design: a liveness endpoint can still return `200` while the dependency is down, but readiness is stricter.

The `/readyz` endpoint performs:
- a DB connectivity attempt
- a lightweight `SELECT 1`
- a `503` if the DB cannot be reached

This is fail-closed: no false sense of readiness is advertised while the database is actually unavailable.

## Observability and paging
The service exposes a small but useful Prometheus surface:
- `reservations_confirmed_total`
- `reservations_declined_total{reason=...}`
- `seats_available{show_id=...}`
- request counter middleware metrics

Structured logs are emitted as JSON with a `request_id` and an inbound `X-Request-ID` is honored if provided. I would page on:
- `/readyz` returning `503` for more than a short interval
- a non-zero `5xx` rate
- reconciliation mismatch (`available + held + confirmed != total_seats`)
- surprising metrics drift between DB state and scraped gauges
- rising DB pool timeouts or transaction retries

## AI usage disclosure
I used AI as a coding accelerator, not as a substitute for correctness review. The system design was directed by the explicit requirements around atomicity, per-user limits, idempotency, and fail-closed readiness. I then validated the implementation against the real Postgres transaction semantics and an actual Dockerized burst run rather than accepting the generated result on trust.

The important part is that the concurrency guarantees were checked with live behavior: hot-seat winner count, idempotency replay behavior, 4xx declines instead of 5xx, and reconciliation after the burst. I did not treat the AI output as final until it was tested against the exact conditions the service must satisfy.

## What I would do next
If this were extended for a production launch, the next steps would be:
- add signed JWT verification for real identity instead of token-string identity
- add a hold/expiry model with a sweeper if the product requires temporary reservation holds
- add a stricter test suite around the DB transaction behavior and concurrency edge cases
- add SLO alerts for readyz failures, 5xx rates, and reconciliation drift
- add queueing or admission control under extremely large ticket drops

The simple and safe model that is already implemented here is solid: Postgres as the source of truth, failure is declared as a 4xx domain outcome, and readiness is only announced when the database dependency is actually reachable.
