# app/routers/reports.py
from fastapi import APIRouter, HTTPException
from app.state import reconciliation_store, invoice_store

router = APIRouter(prefix="/reports", tags=["Report"])


@router.get("/reconciliation")
def list_reconciliation_reports():
    """
    Restituisce tutti i report di riconciliazione prodotti nella sessione corrente.
    Un report compare solo dopo aver chiamato POST /invoices/{id}/reconcile.
    """
    results = []
    for invoice_id, report in reconciliation_store.items():
        invoice = invoice_store.get(invoice_id)
        results.append({
            "invoice_id": invoice_id,
            "filename": invoice.filename if invoice else "n/a",
            "carrier_name": (
                invoice.extraction.carrier_name
                if invoice and invoice.extraction
                else "n/a"
            ),
            "has_discrepancies": report["has_discrepancies"],
            "summary": report["summary"],
        })

    return results


@router.get("/reconciliation/summary")
def reconciliation_summary():
    """
    Aggregato globale di tutte le riconciliazioni effettuate nella sessione.
    Utile per avere una visione d'insieme: quante fatture hanno discrepanze,
    il delta EUR totale e il dettaglio per carrier.
    """

    if not reconciliation_store:
        return {
            "total_reconciled": 0,
            "with_discrepancies": 0,
            "without_discrepancies": 0,
            "total_amount_discrepancies": 0,
            "total_only_in_invoice": 0,
            "total_only_in_database": 0,
            "total_delta_eur": 0.0,
            "by_carrier": {},
        }

    total_reconciled = len(reconciliation_store)
    with_discrepancies = sum(
        1 for r in reconciliation_store.values() if r["has_discrepancies"]
    )
    total_amount_disc = sum(
        r["summary"]["amount_discrepancies_count"]
        for r in reconciliation_store.values()
    )
    total_only_invoice = sum(
        r["summary"]["only_in_invoice_count"]
        for r in reconciliation_store.values()
    )
    total_only_db = sum(
        r["summary"]["only_in_database_count"]
        for r in reconciliation_store.values()
    )

    # Somma del delta EUR su tutte le discrepanze di importo
    total_delta = 0.0
    for report in reconciliation_store.values():
        for disc in report["amount_discrepancies"]:
            total_delta += disc.get("delta_eur", 0.0)

    # Breakdown per carrier
    by_carrier: dict[str, dict] = {}
    for invoice_id, report in reconciliation_store.items():
        invoice = invoice_store.get(invoice_id)
        carrier = (
            invoice.extraction.carrier_name
            if invoice and invoice.extraction
            else "unknown"
        )
        if carrier not in by_carrier:
            by_carrier[carrier] = {
                "invoices_count": 0,
                "with_discrepancies": 0,
                "amount_discrepancies_count": 0,
                "delta_eur": 0.0,
            }
        by_carrier[carrier]["invoices_count"] += 1
        if report["has_discrepancies"]:
            by_carrier[carrier]["with_discrepancies"] += 1
        by_carrier[carrier]["amount_discrepancies_count"] += report["summary"][
            "amount_discrepancies_count"
        ]
        for disc in report["amount_discrepancies"]:
            by_carrier[carrier]["delta_eur"] += disc.get("delta_eur", 0.0)

    # Arrotonda i delta EUR
    total_delta = round(total_delta, 2)
    for c in by_carrier.values():
        c["delta_eur"] = round(c["delta_eur"], 2)

    return {
        "total_reconciled": total_reconciled,
        "with_discrepancies": with_discrepancies,
        "without_discrepancies": total_reconciled - with_discrepancies,
        "total_amount_discrepancies": total_amount_disc,
        "total_only_in_invoice": total_only_invoice,
        "total_only_in_database": total_only_db,
        "total_delta_eur": total_delta,
        "by_carrier": by_carrier,
    }


@router.get("/reconciliation/{invoice_id}")
def get_reconciliation_report(invoice_id: int):
    """
    Restituisce il report di riconciliazione completo per una singola fattura.
    Errore 404 se la riconciliazione non è ancora stata eseguita per quella fattura.
    """

    if invoice_id not in reconciliation_store:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Nessun report disponibile per la fattura {invoice_id}. "
                "Eseguire prima POST /invoices/{invoice_id}/reconcile."
            ),
        )
    return reconciliation_store[invoice_id]