"""Load test for checkout and payment history.

    make stack            # in one terminal
    make loadtest         # open http://localhost:8089, e.g. 50 users, spawn rate 10

Scenarios:
- CheckoutUser: realistic checkouts; 10% of them double-tap (same Idempotency-Key twice).
- HistoryReader: pages through the history of a reservation with many events.
Record p95s in the README "Metrics" section; only publish numbers you measured.
"""

import random
import uuid
from datetime import date, timedelta

from locust import HttpUser, between, task


def checkout_body(guest: str) -> dict:
    return {
        "guest_id": guest,
        "listing_id": f"listing_{random.randint(1, 500)}",
        "check_in": (date.today() + timedelta(days=random.randint(20, 120))).isoformat(),
        "total_amount": random.randint(10_000, 400_000),
        "currency": "USD",
        "installments": random.choice([1, 1, 2, 3, 4]),
        "cancellation_policy": random.choice(["flexible", "moderate", "strict"]),
        "payment_method": random.choices(
            ["pm_card_ok", "pm_card_insufficient_funds", "pm_card_closed"], weights=[90, 7, 3]
        )[0],
    }


class CheckoutUser(HttpUser):
    wait_time = between(0.5, 2)

    def on_start(self):
        self.guest = f"lt_{uuid.uuid4().hex[:10]}"

    @task
    def checkout(self):
        key, body = uuid.uuid4().hex, checkout_body(self.guest)
        with self.client.post(
            "/checkout", json=body, headers={"Idempotency-Key": key}, name="/checkout", catch_response=True
        ) as r:
            if r.status_code in (201, 202, 402):
                r.success()
        if random.random() < 0.1:  # double tap
            with self.client.post(
                "/checkout", json=body, headers={"Idempotency-Key": key}, name="/checkout (replay)", catch_response=True
            ) as r:
                if r.status_code in (201, 202, 402):
                    r.success()


class HistoryReader(HttpUser):
    wait_time = between(0.2, 1)
    reservation_id: str | None = None

    def on_start(self):
        r = self.client.post(
            "/checkout",
            json=checkout_body(f"hist_{uuid.uuid4().hex[:8]}"),
            headers={"Idempotency-Key": uuid.uuid4().hex},
            name="setup checkout",
        )
        if r.status_code in (201, 202):
            self.reservation_id = r.json()["reservation_id"]

    @task
    def read_history(self):
        if not self.reservation_id:
            return
        cursor = None
        for _ in range(20):
            params = {"limit": 50, **({"cursor": cursor} if cursor else {})}
            page = self.client.get(
                f"/reservations/{self.reservation_id}/payments", params=params, name="/reservations/[id]/payments"
            ).json()
            cursor = page.get("next_cursor")
            if not cursor:
                break
