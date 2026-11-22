"""Payment provider webhooks. The provider retries a delivery until it gets a 2xx, so events can repeat."""
import sqlite3

from . import db


class PaymentError(Exception):
    pass


class Payments:
    def __init__(self, conn):
        self.conn = conn

    def handle_event(self, event, now=None):
        """event: dict(id, type, order_id, amount_cents). Returns the order's status after the event."""
        if event.get("type") != "payment.succeeded":
            return None

        order = self.conn.execute("SELECT * FROM orders WHERE id = ?", (event["order_id"],)).fetchone()
        if order is None:
            raise PaymentError(f"payment for unknown order {event['order_id']}")
        if order["status"] == "cancelled":
            raise PaymentError(f"order {order['id']} is cancelled")

        existing = self.conn.execute(
            "SELECT id FROM payments WHERE event_id = ?", (event["id"],)
        ).fetchone()
        if existing is not None:
            current = self.conn.execute("SELECT status FROM orders WHERE id = ?", (order["id"],)).fetchone()
            return current["status"]

        if order["status"] == "paid":
            raise PaymentError(f"order {order['id']} is already paid")

        amount_cents = event["amount_cents"]
        if amount_cents <= 0:
            raise PaymentError("payment amount must be positive")

        current_paid = self.paid_cents(order["id"])
        if current_paid + amount_cents > order["total_cents"]:
            raise PaymentError(
                f"payment of {amount_cents} exceeds remaining balance {order['total_cents'] - current_paid} for order {order['id']}"
            )

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO payments (order_id, event_id, amount_cents, received_at) VALUES (?, ?, ?, ?)",
                (order["id"], event["id"], amount_cents, db.to_iso(now or db.utcnow())),
            )
            new_paid = self.paid_cents(order["id"])
            status = order["status"]
            if status == "pending" and new_paid >= order["total_cents"]:
                self.conn.execute("UPDATE orders SET status = 'paid' WHERE id = ?", (order["id"],))
                status = "paid"
            self.conn.execute("COMMIT")
            return status
        except sqlite3.IntegrityError:
            self.conn.execute("ROLLBACK")
            current = self.conn.execute("SELECT status FROM orders WHERE id = ?", (order["id"],)).fetchone()
            return current["status"]
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def paid_cents(self, order_id):
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS paid FROM payments WHERE order_id = ?", (order_id,)
        ).fetchone()
        return row["paid"]