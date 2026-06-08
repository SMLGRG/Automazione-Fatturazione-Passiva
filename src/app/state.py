# app/state.py
"""
Store in-memoria condivisi tra routers.
Separati da invoices.py per evitare import circolari con reports.py.
Reset a ogni riavvio del server (nessuna persistenza intenzionale).
"""
from app.models import InvoiceStatus

invoice_store: dict[int, InvoiceStatus] = {}
reconciliation_store: dict[int, dict] = {}
invoice_counter: int = 0
pending_markdown_store: dict[int, str] = {}   # invoice_id → markdown in attesa di sample
pending_carrier_store: dict[int, str] = {}    # invoice_id → carrier_name identificato