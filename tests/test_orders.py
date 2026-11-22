import threading
import unittest
from datetime import datetime, timezone

from tillpoint import db, reports
from tillpoint.api import App
from tillpoint.catalog import Catalog, OutOfStock
from tillpoint.payments import PaymentError


class OrderDeskTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect()
        self.app = App(self.conn)
        self.app.catalog.add_product("MUG", "Mug", 1200, stock=10)
        self.app.catalog.add_product("TEE", "T-shirt", 2500, stock=3)
        self.conn.execute("INSERT INTO coupons (code, kind, value) VALUES ('TEN', 'percent', 10)")

    def test_order_totals_with_tax(self):
        order = self.app.orders.create("ana", [("MUG", 2), ("TEE", 1)], region="CA")
        self.assertEqual(order["subtotal_cents"], 4900)
        self.assertEqual(order["tax_cents"], 355)
        self.assertEqual(order["total_cents"], 5255)
        self.assertEqual(self.app.catalog.get_product("MUG").stock, 8)

    def test_coupon_takes_ten_percent(self):
        order = self.app.orders.create("ana", [("MUG", 1)], coupon_code="TEN")
        self.assertEqual(order["discount_cents"], 120)
        self.assertEqual(order["total_cents"], 1080)

    def test_coupon_reduces_taxable_amount(self):
        # Subtotal 1000, 10% off -> 900 taxable; CA 7.25% -> round_half_up(900 * 725, 10000) = 65
        order = self.app.orders.create("ana", [("MUG", 1)], region="CA", coupon_code="TEN")
        self.assertEqual(order["subtotal_cents"], 1200)
        self.assertEqual(order["discount_cents"], 120)
        self.assertEqual(order["tax_cents"], 78)
        self.assertEqual(order["total_cents"], 1158)

    def test_out_of_stock(self):
        with self.assertRaises(OutOfStock):
            self.app.orders.create("ana", [("TEE", 4)])
        self.assertEqual(self.app.catalog.get_product("TEE").stock, 3)

    def test_cancel_pending_order_restocks(self):
        order = self.app.orders.create("ana", [("TEE", 2)])
        self.assertEqual(self.app.catalog.get_product("TEE").stock, 1)
        self.app.orders.cancel(order["id"])
        self.assertEqual(self.app.catalog.get_product("TEE").stock, 3)

    def test_payment_marks_order_paid_and_ignores_duplicate_event(self):
        order = self.app.orders.create("ana", [("MUG", 1)])
        event = {"id": "evt_1", "type": "payment.succeeded", "order_id": order["id"], "amount_cents": 1200}
        status1, payload1 = self.app.handle("POST", "/webhooks/payments", event)
        status2, payload2 = self.app.handle("POST", "/webhooks/payments", event)
        self.assertEqual((status1, payload1["status"]), (200, "paid"))
        self.assertEqual((status2, payload2["status"]), (200, "paid"))
        self.assertEqual(self.app.payments.paid_cents(order["id"]), 1200)

    def test_second_distinct_payment_on_paid_order_rejected(self):
        order = self.app.orders.create("ana", [("MUG", 1)])
        self.app.handle(
            "POST",
            "/webhooks/payments",
            {"id": "evt_1", "type": "payment.succeeded", "order_id": order["id"], "amount_cents": 1200},
        )
        with self.assertRaises(PaymentError):
            self.app.payments.handle_event(
                {"id": "evt_2", "type": "payment.succeeded", "order_id": order["id"], "amount_cents": 1200}
            )
        self.assertEqual(self.app.payments.paid_cents(order["id"]), 1200)

    def test_cancel_paid_order_restocks(self):
        order = self.app.orders.create("ana", [("TEE", 2)])
        self.app.payments.handle_event(
            {"id": "evt_2", "type": "payment.succeeded", "order_id": order["id"], "amount_cents": 5000}
        )
        self.app.orders.cancel(order["id"])
        self.assertEqual(self.app.catalog.get_product("TEE").stock, 3)

    def test_daily_sales_with_india_offset(self):
        order = self.app.orders.create("ana", [("MUG", 1)])
        # 08:00 IST on 2025-03-10 is 02:30 UTC on 2025-03-10
        morning_ist = datetime(2025, 3, 10, 2, 30, tzinfo=timezone.utc)
        self.app.payments.handle_event(
            {"id": "evt_3", "type": "payment.succeeded", "order_id": order["id"], "amount_cents": 1200},
            now=morning_ist,
        )
        self.assertEqual(
            reports.daily_sales(self.conn, "2025-03-10", utc_offset_minutes=330),
            {"day": "2025-03-10", "revenue_cents": 1200, "orders": 1},
        )
        self.assertEqual(
            reports.daily_sales(self.conn, "2025-03-09", utc_offset_minutes=330),
            {"day": "2025-03-09", "revenue_cents": 0, "orders": 0},
        )

    def test_concurrent_workers_do_not_oversell(self):
        conn = db.connect("file:oversell_test?mode=memory&cache=shared", uri=True)
        try:
            Catalog(conn).add_product("CUP", "Cup", 1000, stock=3)
            successes = []
            failures = []
            lock = threading.Lock()

            def worker(idx):
                c = db.connect("file:oversell_test?mode=memory&cache=shared", uri=True)
                try:
                    App(c).orders.create(f"cust_{idx}", [("CUP", 2)])
                    with lock:
                        successes.append(idx)
                except OutOfStock:
                    with lock:
                        failures.append(idx)
                finally:
                    c.close()

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 2)
            self.assertEqual(Catalog(conn).get_product("CUP").stock, 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()