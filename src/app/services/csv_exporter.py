# app/services/csv_exporter.py
import pandas as pd
from pathlib import Path
from app.models import InvoiceExtraction

CSV_COLUMNS = [
    "invoice_id",
    "invoice_number",
    "invoice_date",
    "carrier_name",
    "tracking_number",
    "shipment_date",
    "sender_name",
    "recipient_name",
    "destination_city",
    "destination_postal_code",
    "destination_country",
    "weight_kg",
    "service_type",
    "total_amount_eur",
]


def export_to_csv(extraction: InvoiceExtraction, output_dir: Path, invoice_id: int) -> Path:
    rows = []
    for shipment in extraction.shipments:
        rows.append({
            "invoice_id": invoice_id,
            "invoice_number": extraction.invoice_number,
            "invoice_date": str(extraction.invoice_date) if extraction.invoice_date else "",
            "carrier_name": extraction.carrier_name,
            "tracking_number": shipment.tracking_number or "",
            "shipment_date": shipment.shipment_date or "",
            "sender_name": shipment.sender_name or "",
            "recipient_name": shipment.recipient_name or "",
            "destination_city": shipment.destination_city or "",
            "destination_postal_code": shipment.destination_postal_code or "",
            "destination_country": shipment.destination_country or "",
            "weight_kg": shipment.weight_kg or "",
            "service_type": shipment.service_type or "",
            "total_amount_eur": shipment.total_amount or "",
        })

    df = pd.DataFrame(rows, columns=CSV_COLUMNS)
    csv_path = output_dir / f"invoice_{invoice_id}.csv"
    df.to_csv(csv_path, index=False, sep=";", decimal=",", encoding="utf-8-sig")
    return csv_path