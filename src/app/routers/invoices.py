# app/routers/invoices.py
import json
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.models import CarrierSample, InvoiceStatus
from app.services import PipelineService
from app.state import (
    invoice_counter,
    invoice_store,
    pending_carrier_store,
    pending_markdown_store,
    reconciliation_store,
)

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "config" / "templates"

router = APIRouter(prefix="/invoices", tags=["Fatture"])


def get_pipeline_service() -> PipelineService:
    return PipelineService(
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
        upload_dir=Path(os.getenv("UPLOAD_DIR", "./uploads")),
        output_dir=Path(os.getenv("OUTPUT_DIR", "./outputs")),
        config_dir=Path(os.getenv("CONFIG_DIR", "./config")) / "carriers",

    )


# ------------------------------------------------------------------ #
#  UPLOAD                                                              #
# ------------------------------------------------------------------ #

@router.post("/upload", response_model=InvoiceStatus, status_code=202)
async def upload_invoice(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    template_version: Optional[str] = Query(
        default=None,
        description="Versione template specifica es. 'v2'. Lascia vuoto per usare l'ultima disponibile.",
    ),
):
    global invoice_counter
    invoice_counter += 1
    invoice_id = invoice_counter

    if not file.filename:
        raise HTTPException(status_code=400, detail="Nome file mancante")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail=f"Solo PDF accettati (ricevuto: {file.filename})")

    upload_dir = Path(os.getenv("UPLOAD_DIR", "./uploads"))
    upload_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = upload_dir / f"{invoice_id}_{file.filename}"
    pdf_path.write_bytes(await file.read())

    invoice_store[invoice_id] = InvoiceStatus(id=invoice_id, filename=file.filename, status="processing")
    service = get_pipeline_service()
    background_tasks.add_task(_run_pipeline, service, invoice_id, pdf_path, template_version)
    return invoice_store[invoice_id]


def _run_pipeline(
    service: PipelineService,
    invoice_id: int,
    pdf_path: Path,
    template_version: Optional[str] = None,
):
    result = service.process_invoice(invoice_id, pdf_path, template_version=template_version)
    invoice_store[invoice_id] = result


# ------------------------------------------------------------------ #
#  LETTURA                                                             #
# ------------------------------------------------------------------ #

@router.get("", response_model=list[InvoiceStatus])
def list_invoices():
    return list(invoice_store.values())


@router.get("/templates")
def list_templates():
    """Lista tutti i template disponibili con versioni e campi estratti."""
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    carriers: dict[str, dict] = {}

    for f in sorted(TEMPLATES_DIR.glob("*.json")):
        m = re.match(r"^(.+)_(v\d+)$", f.stem)
        carrier = m.group(1) if m else f.stem
        version = m.group(2) if m else "latest"

        if carrier not in carriers:
            carriers[carrier] = {"versions": [], "fields": [], "batch_size": 8}
        carriers[carrier]["versions"].append(version)

        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            carriers[carrier]["fields"] = [field["name"] for field in data.get("fields", [])]
            carriers[carrier]["batch_size"] = data.get("batch_size", 8)
        except Exception:
            pass

    return [{"carrier": k, **v} for k, v in carriers.items()]

# ------------------------------------------------------------------ #
#  PENDING — sample senza dover conoscere l'ID                        #
# ------------------------------------------------------------------ #

@router.get("/pending", response_model=list[InvoiceStatus])
def list_pending():
    """Lista tutte le fatture in attesa di sample."""
    return [s for s in invoice_store.values() if s.status == "awaiting_sample"]


@router.post("/pending/sample", response_model=InvoiceStatus)
def submit_pending_sample(
    sample: CarrierSample,
    background_tasks: BackgroundTasks,
    carrier_name_override: Optional[str] = Query(
        default=None,
        description="Nome corriere, es. 'EUROTIR'",
    ),
):
    """
    Manda il sample all'unica fattura in attesa.
    Se ce ne sono più d'una, restituisce 409 con la lista degli ID
    così l'utente può usare POST /{invoice_id}/sample per quella specifica.
    """
    pending = [s for s in invoice_store.values() if s.status == "awaiting_sample"]

    if not pending:
        raise HTTPException(status_code=404, detail="Nessuna fattura in attesa di sample.")

    if len(pending) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Più fatture in attesa. Usa POST /{invoice_id}/sample.",
                "pending_ids": [s.id for s in pending],
                "pending_files": [s.filename for s in pending],
            },
        )

    invoice_id = pending[0].id
    current = pending[0]

    if invoice_id not in pending_markdown_store:
        raise HTTPException(status_code=400, detail="Markdown non disponibile. Ricarica il PDF.")

    markdown = pending_markdown_store[invoice_id]
    raw_carrier = pending_carrier_store.get(invoice_id, "UNKNOWN")
    carrier_name = carrier_name_override.upper().strip() if carrier_name_override else raw_carrier

    invoice_store[invoice_id] = InvoiceStatus(
        id=invoice_id, filename=current.filename, status="processing"
    )

    service = get_pipeline_service()
    background_tasks.add_task(
        _run_pipeline_with_sample,
        service, invoice_id, markdown, carrier_name, sample,
        Path(os.getenv("UPLOAD_DIR", "./uploads")) / f"{invoice_id}_{current.filename}",
    )
    return invoice_store[invoice_id]

@router.get("/{invoice_id}", response_model=InvoiceStatus)
def get_invoice(invoice_id: int):
    if invoice_id not in invoice_store:
        raise HTTPException(status_code=404, detail="Fattura non trovata")
    return invoice_store[invoice_id]


@router.get("/{invoice_id}/csv")
def download_csv(invoice_id: int):
    output_dir = Path(os.getenv("OUTPUT_DIR", "./outputs")) / str(invoice_id)
    csv_path = output_dir / f"invoice_{invoice_id}.csv"
    if not csv_path.exists():
        raise HTTPException(status_code=404, detail="CSV non ancora disponibile")
    return FileResponse(csv_path, media_type="text/csv", filename=csv_path.name)


@router.get("/{invoice_id}/markdown")
def get_markdown(invoice_id: int):
    output_dir = Path(os.getenv("OUTPUT_DIR", "./outputs")) / str(invoice_id)
    md_files = list(output_dir.glob("*.md"))
    if not md_files:
        raise HTTPException(status_code=404, detail="Markdown non disponibile")
    return {"markdown": md_files[0].read_text(encoding="utf-8")}


# ------------------------------------------------------------------ #
#  RICONCILIAZIONE                                                     #
# ------------------------------------------------------------------ #

@router.post("/{invoice_id}/reconcile")
def reconcile_invoice(invoice_id: int):
    if invoice_id not in invoice_store:
        raise HTTPException(status_code=404, detail="Fattura non trovata")
    output_dir = Path(os.getenv("OUTPUT_DIR", "./outputs")) / str(invoice_id)
    csv_path = output_dir / f"invoice_{invoice_id}.csv"
    if not csv_path.exists():
        raise HTTPException(status_code=400, detail="CSV non disponibile, elaborare prima la fattura")
    from app.services.reconciler import reconcile
    report = reconcile(csv_path, invoice_id)
    reconciliation_store[invoice_id] = report.to_dict()
    return reconciliation_store[invoice_id]


# ------------------------------------------------------------------ #
#  SAMPLE — endpoint unificato (sostituisce /sample e /sample/v2)     #
# ------------------------------------------------------------------ #

@router.post("/{invoice_id}/sample", response_model=InvoiceStatus)
def submit_sample(
    invoice_id: int,
    sample: CarrierSample,
    background_tasks: BackgroundTasks,
    carrier_name_override: Optional[str] = Query(
        default=None,
        description="Nome corriere da usare se non rilevato automaticamente, es. 'GALARDI'",
    ),
):
    """
    Riceve il campione utente per una fattura in stato 'awaiting_sample'.
    Accetta sia il formato semplice (valori stringa) sia il formato ricco
    (con description e hint per ogni campo) — il modello CarrierSample
    normalizza entrambi automaticamente.
    """
    if invoice_id not in invoice_store:
        raise HTTPException(status_code=404, detail="Fattura non trovata")

    current = invoice_store[invoice_id]
    if current.status != "awaiting_sample":
        raise HTTPException(
            status_code=400,
            detail=f"La fattura {invoice_id} non è in attesa di sample (stato: {current.status})",
        )
    if invoice_id not in pending_markdown_store:
        raise HTTPException(
            status_code=400,
            detail="Markdown non disponibile per questa fattura. Ricarica il PDF.",
        )

    markdown = pending_markdown_store[invoice_id]
    raw_carrier = pending_carrier_store.get(invoice_id, "UNKNOWN")
    carrier_name = carrier_name_override.upper().strip() if carrier_name_override else raw_carrier

    invoice_store[invoice_id] = InvoiceStatus(
        id=invoice_id, filename=current.filename, status="processing"
    )

    service = get_pipeline_service()
    background_tasks.add_task(
        _run_pipeline_with_sample,
        service, invoice_id, markdown, carrier_name, sample,
        Path(os.getenv("UPLOAD_DIR", "./uploads")) / f"{invoice_id}_{current.filename}",
    )
    return invoice_store[invoice_id]


def _run_pipeline_with_sample(
    service: PipelineService,
    invoice_id: int,
    markdown: str,
    carrier_name: str,
    sample: CarrierSample,
    pdf_path: Path,
):
    result = service.process_invoice_with_sample(
        invoice_id, pdf_path, markdown, carrier_name, sample
    )
    invoice_store[invoice_id] = result
    pending_markdown_store.pop(invoice_id, None)
    pending_carrier_store.pop(invoice_id, None)