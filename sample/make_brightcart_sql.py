"""Deterministic generator for sample/brightcart.sql (made-up data). Run: python sample/make_brightcart_sql.py"""
import random
from datetime import date, timedelta
from pathlib import Path

rng = random.Random(42)
END = date(2026, 11, 7)
CATS = {
    "Refunds": ["Refund not received", "Partial refund", "Refund delay"],
    "Payments": ["Card declined", "Double charge", "Invoice request"],
    "Delivery": ["Late delivery", "Lost parcel", "Wrong address"],
    "Returns": ["Return label", "Damaged item", "Wrong size"],
}
FIRST = ["Ava", "Liam", "Noah", "Mia", "Zoe", "Ethan", "Ivy", "Leo", "Nina", "Omar", "Priya", "Sam"]
LAST = ["Stone", "Reed", "Khan", "Lopez", "Chen", "Novak", "Bell", "Ortiz", "Singh", "Park"]
rows, n = [], 0
for back in range(29, -1, -1):
    d = END - timedelta(days=back)
    for q, base in {"Refunds": 20, "Payments": 30, "Delivery": 28, "Returns": 22}.items():
        k = round(base * (3 if q == "Refunds" and back < 7 else 1) * rng.uniform(0.85, 1.15))
        for _ in range(k):
            n += 1
            f, l = rng.choice(FIRST), rng.choice(LAST)
            cat = rng.choice(CATS[q])
            status = rng.choices(["Open", "Pending", "Resolved", "Closed"], [2, 2, 3, 3])[0]
            res = "NULL" if status in ("Open", "Pending") else round(rng.uniform(1, 72), 1)
            rows.append(
                f"({n},'{d}','{q}','{cat}','{status}','{rng.choice(['Low','Medium','High','Urgent'])}',"
                f"'{cat} #{n}','{f} {l}','{f.lower()}.{l.lower()}{n}@example.com',"
                f"{round(rng.uniform(0.1, 12), 1)},{res})"
            )
sql = (
    "CREATE TABLE tickets (id INTEGER PRIMARY KEY, created_at TEXT, queue TEXT, category TEXT, status TEXT, "
    "priority TEXT, subject TEXT, customer_name TEXT, email TEXT, first_response_time_hrs REAL, resolution_time_hrs REAL);\n"
    "INSERT INTO tickets VALUES\n" + ",\n".join(rows) + ";\n"
)
Path(__file__).with_name("brightcart.sql").write_text(sql)
print(n, "tickets")
