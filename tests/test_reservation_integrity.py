import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.routes import app


def bearer(user):
    return {"Authorization": f"Bearer {user}"}


class ReservationIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    def create_show(self, seats):
        response = self.client.post(
            "/shows",
            headers=bearer("admin-secret"),
            json={
                "name": f"integrity-{uuid.uuid4().hex}",
                "seats": seats,
                "price_paise": 100,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def reserve(self, show_id, user, seats, key):
        return self.client.post(
            f"/shows/{show_id}/reserve",
            headers={**bearer(user), "Idempotency-Key": key},
            json={"seats": seats},
        )

    def test_multi_seat_decline_does_not_partially_reserve(self):
        show_id = self.create_show(["A1", "A2"])
        first = self.reserve(show_id, "alice", ["A1"], "alice-a1")
        self.assertEqual(first.status_code, 201, first.text)

        partial = self.reserve(show_id, "bob", ["A1", "A2"], "bob-a1-a2")
        self.assertEqual(partial.status_code, 409, partial.text)
        self.assertEqual(partial.json()["error"], "seat_taken")

        state = self.client.get(f"/shows/{show_id}").json()
        self.assertEqual(state["seats"], {"A1": "confirmed", "A2": "available"})
        self.assertEqual(state["available"] + state["held"] + state["confirmed"], 2)
        self.assertTrue(state["reconciled"])

    def test_concurrent_cancel_and_rebook_never_double_assigns_seat(self):
        show_id = self.create_show(["A1"])
        initial = self.reserve(show_id, "alice", ["A1"], "alice-initial")
        self.assertEqual(initial.status_code, 201, initial.text)
        reservation_id = initial.json()["reservation_id"]

        start = threading.Barrier(2)

        def cancel():
            start.wait()
            return self.client.post(
                f"/reservations/{reservation_id}/cancel",
                headers=bearer("alice"),
            )

        def rebook():
            start.wait()
            return self.reserve(show_id, "bob", ["A1"], "bob-rebook")

        with ThreadPoolExecutor(max_workers=2) as executor:
            cancel_future = executor.submit(cancel)
            reserve_future = executor.submit(rebook)
            cancel_response = cancel_future.result()
            reserve_response = reserve_future.result()

        self.assertEqual(cancel_response.status_code, 200, cancel_response.text)
        if reserve_response.status_code == 409:
            self.assertEqual(reserve_response.json()["error"], "seat_taken")
            reserve_response = self.reserve(show_id, "bob", ["A1"], "bob-rebook")
        self.assertEqual(reserve_response.status_code, 201, reserve_response.text)

        state = self.client.get(f"/shows/{show_id}").json()
        self.assertEqual(state["seats"], {"A1": "confirmed"})
        self.assertEqual(state["confirmed"], 1)
        self.assertEqual(state["available"] + state["held"] + state["confirmed"], 1)
        self.assertTrue(state["reconciled"])


if __name__ == "__main__":
    unittest.main()