# app/services/csv_exporter.py
import logging
import time
from pathlib import Path

import pandas as pd

from app.models import InvoiceExtraction

logger = logging.getLogger(__name__)

# Colonne di testata: usate nel fallback (nessun requested_fields)
_HEADER_COLUMNS = ["invoice_id", "invoice_number", "invoice_date", "carrier_name"]

# Colonne core spedizione: usate nel fallback
# total_amount (nome nel modello) → total_amount_eur (nome nel CSV)
_CORE_SHIPMENT_COLUMNS = [
    "tracking_number",
    "shipment_date",
    "sender_name",
    "recipient_name",
    "destination_city",
    "destination_postal_code",
    "weight_kg",
    "total_amount_eur",
]


def export_to_csv(
    extraction: InvoiceExtraction,
    output_dir: Path,
    invoice_id: int,
    requested_fields: list[str] | None = None,
) -> Path:
    """
    Esporta i dati estratti in CSV semicolon-separated, UTF-8 BOM (compatibile Excel).

    Se requested_fields è fornito (letto dal template), il CSV contiene solo
    le colonne richieste dall'utente nel sample + invoice_id.
    Altrimenti usa tutte le colonne core + eventuali extra da model_extra.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    invoice_number = extraction.invoice_number or ""
    invoice_date = str(extraction.invoice_date) if extraction.invoice_date else ""
    carrier_name = extraction.carrier_name or ""

    shipments = extraction.shipments or []

    # --- DETERMINA LE COLONNE ---
    HEADER_FIELD_NAMES = {"invoice_number", "invoice_date", "total_invoice_amount"}

    if requested_fields:
        # Solo le colonne richieste dall'utente nel sample
        header_cols = ["invoice_id"]
        for f in requested_fields:
            if f in HEADER_FIELD_NAMES:
                header_cols.append(f)  # includi solo se l'utente le ha chieste

        shipment_cols = [
            "total_amount_eur" if f == "total_amount" else f
            for f in requested_fields
            if f not in HEADER_FIELD_NAMES
        ]
        all_columns = header_cols + shipment_cols
    else:
        # Fallback: colonne core + extras da model_extra
        extra_cols: set[str] = set()
        for s in shipments:
            if s and hasattr(s, "model_extra") and s.model_extra:
                extra_cols.update(s.model_extra.keys())
        all_columns = _HEADER_COLUMNS + _CORE_SHIPMENT_COLUMNS + sorted(extra_cols)

    # --- COSTRUISCI LE RIGHE ---
    rows = []

    if not shipments:
        logger.warning(f"[Invoice {invoice_id}] Nessuna spedizione trovata. Generazione riga vuota.")
        rows.append({col: "" for col in all_columns} | {"invoice_id": invoice_id})
    else:
        for shipment in shipments:
            data = shipment.model_dump()

            # Base row con tutti i valori possibili
            full_row: dict = {
                "invoice_id": invoice_id,
                "invoice_number": invoice_number,
                "invoice_date": invoice_date,
                "carrier_name": carrier_name,
                "tracking_number": data.get("tracking_number") or "",
                "shipment_date": data.get("shipment_date") or "",
                "sender_name": data.get("sender_name") or "",
                "recipient_name": data.get("recipient_name") or "",
                "destination_city": data.get("destination_city") or "",
                "destination_postal_code": data.get("destination_postal_code") or "",
                "weight_kg": data.get("weight_kg"),
                "total_amount_eur": data.get("total_amount"),
            }

            # Aggiungi eventuali campi extra da model_extra
            for k, v in (data.get("__pydantic_extra__") or {}).items():
                full_row[k] = v or ""
            # Pydantic v2: model_extra è accessibile direttamente
            if hasattr(shipment, "model_extra") and shipment.model_extra:
                for k, v in shipment.model_extra.items():
                    full_row[k] = v or ""

            # Filtra solo le colonne previste
            row = {col: full_row.get(col, "") for col in all_columns}
            rows.append(row)

    # --- WARNINGS ---
    empty_tracking = sum(1 for r in rows if not r.get("tracking_number"))
    empty_amount = sum(1 for r in rows if r.get("total_amount_eur") is None)
    if empty_tracking:
        logger.warning(f"[Invoice {invoice_id}] {empty_tracking}/{len(rows)} righe senza tracking_number")
    if empty_amount:
        logger.warning(f"[Invoice {invoice_id}] {empty_amount}/{len(rows)} righe senza total_amount_eur")

    # --- ESPORTA ---
    df = pd.DataFrame(rows, columns=all_columns)
    csv_path = output_dir / f"invoice_{invoice_id}.csv"

    try:
        df.to_csv(csv_path, index=False, sep=";", decimal=",", encoding="utf-8-sig")
        logger.info(
            f"[Invoice {invoice_id}] CSV esportato: {csv_path} "
            f"({len(rows)} righe, colonne: {all_columns})"
        )
    except PermissionError:
        timestamp = int(time.time())
        csv_path = output_dir / f"invoice_{invoice_id}_{timestamp}.csv"
        logger.warning(f"[Invoice {invoice_id}] File bloccato (Excel aperto?). Emergenza: {csv_path}")
        df.to_csv(csv_path, index=False, sep=";", decimal=",", encoding="utf-8-sig")

    return csv_path