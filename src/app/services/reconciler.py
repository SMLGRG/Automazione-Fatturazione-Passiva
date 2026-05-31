# app/services/reconciler.py
import logging
import pandas as pd
from pathlib import Path
from sqlalchemy import create_engine, text
import os

logger = logging.getLogger(__name__)


class ReconciliationReport:
    def __init__(self, invoice_id: int):
        self.invoice_id = invoice_id
        self.amount_discrepancies: list[dict] = []
        self.only_in_invoice: list[dict] = []
        self.only_in_database: list[dict] = []

    @property
    def has_discrepancies(self) -> bool:
        return bool(self.amount_discrepancies or self.only_in_invoice or self.only_in_database)

    def to_dict(self) -> dict:
        return {
            "invoice_id": self.invoice_id,
            "has_discrepancies": self.has_discrepancies,
            "amount_discrepancies": self.amount_discrepancies,
            "only_in_invoice": self.only_in_invoice,
            "only_in_database": self.only_in_database,
            "summary": {
                "amount_discrepancies_count": len(self.amount_discrepancies),
                "only_in_invoice_count": len(self.only_in_invoice),
                "only_in_database_count": len(self.only_in_database),
            }
        }


def reconcile(csv_path: Path, invoice_id: int) -> ReconciliationReport:
    report = ReconciliationReport(invoice_id)
    df_invoice = pd.read_csv(csv_path, sep=";", decimal=",")
    engine = create_engine(os.getenv("DATABASE_URL", "sqlite:///./app.db"))
    tracking_list = df_invoice["tracking_number"].tolist()
    placeholders = ", ".join([f"'{t}'" for t in tracking_list])

    query = text(f"""
        SELECT tracking_number, cost_eur, shipment_date
        FROM shipments
        WHERE tracking_number IN ({placeholders})
    """)

    with engine.connect() as conn:
        df_db = pd.read_sql(query, conn)

    merged = pd.merge(
        df_invoice[["tracking_number", "total_amount_eur"]],
        df_db[["tracking_number", "cost_eur"]],
        on="tracking_number", how="outer", indicator=True
    )

    only_invoice = merged[merged["_merge"] == "left_only"]
    report.only_in_invoice = only_invoice[["tracking_number", "total_amount_eur"]].to_dict("records")

    only_db = merged[merged["_merge"] == "right_only"]
    report.only_in_database = only_db[["tracking_number", "cost_eur"]].to_dict("records")

    both = merged[merged["_merge"] == "both"].copy()
    both["delta_eur"] = both["total_amount_eur"] - both["cost_eur"]
    discrepancies = both[both["delta_eur"].abs() > 0.01]

    for _, row in discrepancies.iterrows():
        report.amount_discrepancies.append({
            "tracking_number": row["tracking_number"],
            "invoiced_amount_eur": row["total_amount_eur"],
            "database_amount_eur": row["cost_eur"],
            "delta_eur": round(row["delta_eur"], 2),
        })

    logger.info(f"[Invoice {invoice_id}] Riconciliazione: "
                f"{len(report.amount_discrepancies)} discrepanze importo, "
                f"{len(report.only_in_invoice)} solo in fattura, "
                f"{len(report.only_in_database)} solo in DB")
    return report