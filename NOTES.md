# NOTES

## 1. Report – Duplicate payment notifications

**Status:** fixed

**What I saw:** Retried `payment.succeeded` webhook events with the same `event["id"]` were inserted again as new rows in `payments`, inflating `paid_cents` and daily revenue.

**Root cause:** `payments.event_id` had no uniqueness constraint, and `Payments.handle_event()` did not check for or ignore already-processed event IDs.

**Fix and why:** Added a `UNIQUE` constraint on `payments.event_id` in `tillpoint/db.py` and checked for existing `event_id`s (handling `sqlite3.IntegrityError` inside the transaction) in `tillpoint/payments.py` so duplicate webhook deliveries are idempotent.

**How I verified it:** Added a unit test sending the same `payment.succeeded` event twice and verified the order becomes `paid` once without double-counting `paid_cents`.

**Commits:** <hash-from-git-log>

## 2. Report – Stock not restored after abandoned checkout

**Status:** fixed

**What I saw:** Creating an order reserved stock immediately, while cancelling a `pending` order only released stock if the order status was `paid`.

**Root cause:** `Orders.cancel()` checked `if order["status"] == "paid":` before calling `self.catalog.release(...)`, leaving stock tied up on cancelled `pending` orders.

**Fix and why:** Updated `Orders.cancel()` in `tillpoint/orders.py` to release reserved stock for both `pending` and `paid` orders while still blocking cancellation of `shipped` orders and handling already-`cancelled` orders cleanly.

**How I verified it:** Added a test creating a `pending` order, cancelling it, and asserting that the reserved stock returns to its original value.

**Commits:** <hash-from-git-log>

## 3. Report – Incorrect tax with coupons

**Status:** fixed

**What I saw:** Orders with a discount coupon in taxed regions had higher tax than expected because tax was calculated on the pre-discount subtotal.

**Root cause:** `pricing.price_order()` passed `sub` (full subtotal) into `tax_for()` instead of subtracting `discount` first.

**Fix and why:** Calculated `taxable = max(0, sub - discount)` in `tillpoint/pricing.py` and computed tax and total from `taxable` so coupons reduce the taxable amount.

**How I verified it:** Added a test applying a 10% coupon in region `CA` and checked `discount_cents`, `tax_cents`, and `total_cents`.

**Commits:** <hash-from-git-log>

## 4. Report – India timezone issue

**Status:** fixed

**What I saw:** Morning sales in India (`UTC+5:30`, such as `08:00 IST` = `02:30 UTC`) fell outside the expected UTC window or landed on the wrong report day.

**Root cause:** `reports.day_bounds_utc()` added `utc_offset_minutes` to local midnight instead of subtracting it to get the UTC instant (`UTC = local - offset`).

**Fix and why:** Changed `start += timedelta(minutes=utc_offset_minutes)` to `start -= timedelta(minutes=utc_offset_minutes)` in `tillpoint/reports.py`.

**How I verified it:** Added a test using a morning `UTC+5:30` timestamp (`utc_offset_minutes=330`) and verified it counts toward that local day and `0` on the previous day.

**Commits:** <hash-from-git-log>

## 5. Report – Negative stock with multiple workers

**Status:** fixed

**What I saw:** Under concurrent workers, stock could be read as available by multiple callers and decremented below zero, and per-process caching could serve stale product state.

**Root cause:** `Catalog.reserve()` read stock and ran an unconditional `UPDATE` in two steps, and `Catalog._cache` was an un-shared in-memory dict.

**Fix and why:** Removed the stale in-process cache in `tillpoint/catalog.py` and made `Catalog.reserve()` execute an atomic `UPDATE products SET stock = stock - ? WHERE sku = ? AND stock >= ?`, raising `OutOfStock` when `rowcount == 0` (along with an atomic `max_uses` guard on coupons in `tillpoint/orders.py`).

**How I verified it:** Added a multithreaded test using shared-cache connections where multiple workers attempt to reserve stock concurrently and verified no overselling or negative stock occurs.

**Commits:** <hash-from-git-log>

## 6. Open question – Genuine duplicate payments

**My decision:** Reject any new `payment.succeeded` event (distinct `event["id"]`) once an order is already `paid`, or if the payment would push total paid cents above `order["total_cents"]`, by raising `PaymentError` (which `App.handle()` maps to HTTP `409`).

**Why:** Rejecting extra/overpaying distinct payments is the smallest safe behavior that prevents silent over-collection and keeps `paid_cents` within `total_cents` without inventing unverified refund or credit workflows.

**What it costs:** If a customer genuinely pays twice at the provider side, the webhook is rejected with `409` rather than automatically issuing a refund or tracking an overpayment ledger/credit balance; ops/support would still handle any real provider-side duplicate charge manually.

**What I built:** In `tillpoint/payments.py`, after passing the `event_id` idempotency check, `handle_event()` raises `PaymentError` if `order["status"] == "paid"` or if `current_paid + amount_cents > order["total_cents"]`.

**How I verified it:** Added a unit test sending a second distinct payment event (`evt_2`) after an order is already `paid` and verified `PaymentError` is raised while `paid_cents` remains unchanged.

**Commits:** <hash-from-git-log>

## Other things I noticed

- Abandoned `pending` orders that are never explicitly cancelled do not automatically expire and release stock.
- `Orders.ship()` updates order status directly without wrapping in a transaction or checking stock/payment invariants beyond `status == "paid"`.

## Session logs

- AI assistant used: exported conversation log saved in `session_logs/` (with any secrets/API keys/personal data removed if present).
