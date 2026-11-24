import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Literal
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

DB_PATH = "tracker.db"
API_SECRET_KEY = os.getenv("API_KEY", "test-secret-key")
MIN_MARGIN_THRESHOLD = 20.0  # Triggers alert if gross margin drops below 20%


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@asynccontextmanager
async def lifespan(app: FastAPI):
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_type TEXT NOT NULL,
                vendor_name TEXT NOT NULL,
                transaction_date TEXT NOT NULL,
                total_amount REAL NOT NULL,
                tax_amount REAL DEFAULT 0.0,
                category TEXT NOT NULL,
                payment_method TEXT NOT NULL,
                confidence_score REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    yield


app = FastAPI(title="Business Margin & Expense Tracker API", lifespan=lifespan)


class TransactionIn(BaseModel):
    record_type: Literal["sale", "expense"]
    vendor_name: str
    transaction_date: str
    total_amount: float = Field(gt=0)
    tax_amount: float = Field(ge=0, default=0.0)
    category: str
    payment_method: str
    confidence_score: float = Field(ge=0.0, le=1.0)


@app.post("/api/v1/ingest")
def ingest_transaction(
    payload: TransactionIn,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    # 1. Verify Make.com's X-API-Key header
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

    with get_db() as conn:
        # 2. Check historical vendor average before inserting (for spike detection)
        vendor_avg_row = conn.execute(
            """
            SELECT AVG(total_amount) AS avg_amt, COUNT(*) AS cnt
            FROM transactions
            WHERE record_type = 'expense' AND LOWER(vendor_name) = LOWER(?)
            """,
            (payload.vendor_name,),
        ).fetchone()

        # 3. Insert the new transaction
        cursor = conn.execute(
            """
            INSERT INTO transactions (
                record_type, vendor_name, transaction_date, total_amount,
                tax_amount, category, payment_method, confidence_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.record_type,
                payload.vendor_name,
                payload.transaction_date,
                payload.total_amount,
                payload.tax_amount,
                payload.category,
                payload.payment_method,
                payload.confidence_score,
            ),
        )
        conn.commit()
        tx_id = cursor.lastrowid

        # 4. Recalculate overall revenue, expenses, and gross margin
        totals = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN record_type = 'sale' THEN total_amount ELSE 0 END), 0) AS revenue,
                COALESCE(SUM(CASE WHEN record_type = 'expense' THEN total_amount ELSE 0 END), 0) AS expenses
            FROM transactions
            """
        ).fetchone()

    revenue = float(totals["revenue"])
    expenses = float(totals["expenses"])
    net_profit = revenue - expenses
    gross_margin_pct = round((net_profit / revenue) * 100, 2) if revenue > 0 else 0.0

    # 5. Evaluate anomaly rules
    alerts = []

    if revenue > 0 and gross_margin_pct < MIN_MARGIN_THRESHOLD:
        alerts.append(
            f"Margin Drop Warning: Gross margin fell to {gross_margin_pct}% "
            f"(below your {MIN_MARGIN_THRESHOLD}% threshold)."
        )

    if (
        payload.record_type == "expense"
        and vendor_avg_row["cnt"] >= 2
        and payload.total_amount > (vendor_avg_row["avg_amt"] * 1.5)
    ):
        alerts.append(
            f"Vendor Cost Spike: {payload.vendor_name} charge ({payload.total_amount:,.2f}) "
            f"is >50% above your historical average ({vendor_avg_row['avg_amt']:,.2f})."
        )

    anomaly_detected = len(alerts) > 0
    alert_message = " | ".join(alerts) if anomaly_detected else "Normal transaction within margin targets."

    return {
        "status": "saved",
        "transaction_id": tx_id,
        "metrics": {
            "total_revenue": round(revenue, 2),
            "total_expenses": round(expenses, 2),
            "net_profit": round(net_profit, 2),
            "gross_margin_pct": gross_margin_pct,
        },
        "anomaly_detected": anomaly_detected,
        "alert_message": alert_message,
    }


@app.get("/api/v1/summary")
def get_summary():
    with get_db() as conn:
        totals = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN record_type = 'sale' THEN total_amount ELSE 0 END), 0) AS revenue,
                COALESCE(SUM(CASE WHEN record_type = 'expense' THEN total_amount ELSE 0 END), 0) AS expenses,
                COUNT(*) AS total_count
            FROM transactions
            """
        ).fetchone()
        recent = conn.execute(
            "SELECT * FROM transactions ORDER BY id DESC LIMIT 10"
        ).fetchall()

    revenue = float(totals["revenue"])
    expenses = float(totals["expenses"])
    net_profit = revenue - expenses
    margin = round((net_profit / revenue) * 100, 2) if revenue > 0 else 0.0

    return {
        "total_transactions": totals["total_count"],
        "total_revenue": round(revenue, 2),
        "total_expenses": round(expenses, 2),
        "net_profit": round(net_profit, 2),
        "gross_margin_pct": margin,
        "recent_transactions": [dict(row) for row in recent],
    }