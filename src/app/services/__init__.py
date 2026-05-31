# app/services/__init__.py
import logging
from pathlib import Path
from pydantic import ValidationError

from app.models import InvoiceExtraction, InvoiceStatus
from app.parsers.factory import get_parser, detect_carrier
from app.extractors import OllamaExtractor

logger = logging.getLogger(__name__)


class PipelineService:
    """
    Coordina tutte le fasi della pipeline per una singola fattura:
    PDF → Markdown → JSON → Validazione
    """

    def __init__(self, ollama_base_url: str, ollama_model: str,
                 upload_dir: Path, output_dir: Path, config_dir: Path):
        self.extractor = OllamaExtractor(ollama_base_url, ollama_model)
        self.upload_dir = upload_dir
        self.output_dir = output_dir
        self.config_dir = config_dir

    def process_invoice(self, invoice_id: int, pdf_path: Path) -> InvoiceStatus:
        """
        Elabora una fattura PDF dall'inizio alla fine.
        Non propaga eccezioni: le cattura tutte e le registra nell'InvoiceStatus.
        """
        logger.info(f"[Invoice {invoice_id}] Inizio elaborazione: {pdf_path.name}")
        output_dir = self.output_dir / str(invoice_id)
        output_dir.mkdir(parents=True, exist_ok=True)

        # --- FASE 1: PDF → Markdown ---
        try:
            parser = get_parser("unknown")
            markdown = parser.parse(pdf_path, output_dir)
            logger.info(f"[Invoice {invoice_id}] Conversione Docling completata "
                        f"({len(markdown)} caratteri)")
        except Exception as e:
            logger.error(f"[Invoice {invoice_id}] Errore Docling: {e}", exc_info=True)
            return InvoiceStatus(
                id=invoice_id,
                filename=pdf_path.name,
                status="error",
                error_field="pdf_parsing",
                error_message=str(e),
            )

        # Rilevamento trasportatore e re-processing con parser specifico
        carrier_name = detect_carrier(markdown, self.config_dir)
        logger.info(f"[Invoice {invoice_id}] Trasportatore rilevato: {carrier_name}")
        parser = get_parser(carrier_name)
        markdown = parser.post_process(markdown)

        # --- FASE 2: Markdown → JSON ---
        try:
            extraction: InvoiceExtraction = self.extractor.extract(markdown)
            logger.info(f"[Invoice {invoice_id}] Estrazione AI completata — "
                        f"fattura {extraction.invoice_number}, "
                        f"{len(extraction.shipments)} spedizioni")
        except RuntimeError as e:
            logger.error(f"[Invoice {invoice_id}] Errore Ollama: {e}")
            return InvoiceStatus(
                id=invoice_id,
                filename=pdf_path.name,
                status="error",
                error_field="llm_extraction",
                error_message=str(e),
            )
        except (ValueError, ValidationError) as e:
            logger.warning(f"[Invoice {invoice_id}] Validazione parziale: {e}")
            return InvoiceStatus(
                id=invoice_id,
                filename=pdf_path.name,
                status="review_required",
                error_field="validation",
                error_message=str(e),
            )
        except Exception as e:
            logger.error(f"[Invoice {invoice_id}] Errore imprevisto: {e}", exc_info=True)
            return InvoiceStatus(
                id=invoice_id,
                filename=pdf_path.name,
                status="error",
                error_field="unknown",
                error_message=str(e),
            )
        # --- FASE 3: CSV Export ---
        try:
            from app.services.csv_exporter import export_to_csv
            csv_path = export_to_csv(extraction, output_dir, invoice_id)
            logger.info(f"[Invoice {invoice_id}] CSV generato: {csv_path.name}")
        except Exception as e:
            logger.error(f"[Invoice {invoice_id}] Errore CSV: {e}", exc_info=True)
            return InvoiceStatus(
                id=invoice_id, filename=pdf_path.name, status="error",
                error_field="csv_export", error_message=str(e),
            )
        return InvoiceStatus(
            id=invoice_id,
            filename=pdf_path.name,
            status="completed",
            extraction=extraction,
        )