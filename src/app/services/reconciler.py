# app/services/reconciler.py
import logging
import os
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)
_engine = create_engine(os.getenv("DATABASE_URL", "sqlite:///./app.db"))


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
            },
        }


def reconcile(csv_path: Path, invoice_id: int) -> ReconciliationReport:
    report = ReconciliationReport(invoice_id)
    df_invoice = pd.read_csv(csv_path, sep=";", decimal=",")

    # Validazione immediata — prima di qualsiasi altra operazione
    required_columns = {"tracking_number", "total_amount_eur"}
    missing = required_columns - set(df_invoice.columns)
    if missing:
        logger.error(f"[Invoice {invoice_id}] Colonne mancanti nel CSV: {missing}")
        raise ValueError(f"CSV malformato, colonne mancanti: {missing}")

    tracking_list = df_invoice["tracking_number"].dropna().astype(str).tolist()
    if not tracking_list:
        logger.warning(f"[Invoice {invoice_id}] Nessun tracking number nel CSV, riconciliazione saltata.")
        return report

    query = text("""
        SELECT tracking_number, cost_eur, shipment_date
        FROM shipments
        WHERE tracking_number IN :tracking_list
    """)

    with _engine.connect() as conn:
        df_db = pd.read_sql(query, conn, params={"tracking_list": tuple(tracking_list)})

    if df_db.empty:
        logger.warning(
            f"[Invoice {invoice_id}] Nessun record nel DB per i tracking forniti. "
            f"Tutti i {len(tracking_list)} finiscono in 'only_in_invoice'."
        )

    merged = pd.merge(
        df_invoice[["tracking_number", "total_amount_eur"]],
        df_db[["tracking_number", "cost_eur"]],
        on="tracking_number",
        how="outer",
        indicator=True,
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

    logger.info(
        f"[Invoice {invoice_id}] Riconciliazione completata: "
        f"{len(report.amount_discrepancies)} discrepanze importo, "
        f"{len(report.only_in_invoice)} solo in fattura, "
        f"{len(report.only_in_database)} solo in DB"
    )
    return report