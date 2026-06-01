# app/routers/invoices.py
import os
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse

from app.models import InvoiceStatus
from app.services import PipelineService

router = APIRouter(prefix="/invoices", tags=["Fatture"])

invoice_store: dict[int, InvoiceStatus] = {}
reconciliation_store: dict[int, dict] = {}  # invoice_id → report.to_dict()
invoice_counter = 0


def get_pipeline_service() -> PipelineService:
    return PipelineService(
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:3b"),
        upload_dir=Path(os.getenv("UPLOAD_DIR", "./uploads")),
        output_dir=Path(os.getenv("OUTPUT_DIR", "./outputs")),
        config_dir=Path("./config/carriers"),
    )


@router.post("/upload", response_model=InvoiceStatus, status_code=202)
async def upload_invoice(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    global invoice_counter
    invoice_counter += 1
    invoice_id = invoice_counter

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Il file deve essere un PDF")

    upload_dir = Path(os.getenv("UPLOAD_DIR", "./uploads"))
    upload_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = upload_dir / f"{invoice_id}_{file.filename}"
    pdf_path.write_bytes(await file.read())

    status = InvoiceStatus(id=invoice_id, filename=file.filename, status="processing")
    invoice_store[invoice_id] = status

    service = get_pipeline_service()
    background_tasks.add_task(_run_pipeline, service, invoice_id, pdf_path)
    return status


def _run_pipeline(service: PipelineService, invoice_id: int, pdf_path: Path):
    result = service.process_invoice(invoice_id, pdf_path)
    invoice_store[invoice_id] = result


@router.get("", response_model=list[InvoiceStatus])
def list_invoices():
    return list(invoice_store.values())


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


@router.post("/{invoice_id}/reconcile")
def reconcile_invoice(invoice_id: int):
    if invoice_id not in invoice_store:
        raise HTTPException(status_code=404, detail="Fattura non trovata")
    output_dir = Path(os.getenv("OUTPUT_DIR", "./outputs")) / str(invoice_id)
    csv_path = output_dir / f"invoice_{invoice_id}.csv"
    if not csv_path.exists():
        raise HTTPException(status_code=400, detail="CSV non disponibile")
    from app.services.reconciler import reconcile
    report = reconcile(csv_path, invoice_id)
    result = report.to_dict()
    reconciliation_store[invoice_id] = result  # salva per reports.py
    return result