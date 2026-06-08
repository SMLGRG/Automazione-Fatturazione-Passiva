# src/app/services/__init__.py
import logging
import re
import json
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from app.models import CarrierSample, InvoiceExtraction, InvoiceStatus
from app.parsers.configurable_parser import ConfigurableParser
from app.parsers.factory import get_parser
from app.services.carrier_detector import detect_or_create_carrier
from app.services.csv_exporter import export_to_csv
from app.services.template_generator import generate_template_from_sample
from app.services.carrier_detector import enrich_carrier_yaml

from app.state import pending_carrier_store, pending_markdown_store

logger = logging.getLogger(__name__)


class PipelineService:
    """
    Coordina tutte le fasi della pipeline per una singola fattura:
    PDF → Markdown (Docling) → Rilevamento Carrier → ConfigurableParser → CSV
    """

    def __init__(
        self,
        ollama_base_url: str,
        ollama_model: str,
        upload_dir: Path,
        output_dir: Path,
        config_dir: Path,
        ollama_model_detect: str = "qwen2.5:7b",
    ):
        self.ollama_base_url = ollama_base_url
        self.ollama_model = ollama_model
        self.ollama_model_detect = ollama_model_detect
        self.upload_dir = upload_dir
        self.output_dir = output_dir
        self.config_dir = config_dir

    # ------------------------------------------------------------------ #
    #  PIPELINE PRINCIPALE                                                 #
    # ------------------------------------------------------------------ #

    def process_invoice(
        self,
        invoice_id: int,
        pdf_path: Path,
        template_version: Optional[str] = None,
    ) -> InvoiceStatus:
        """
        Elabora una fattura PDF dall'inizio alla fine.
        Non propaga eccezioni: le cattura tutte e le registra nell'InvoiceStatus.
        """
        logger.info(f"[Invoice {invoice_id}] Avvio pipeline: {pdf_path.name}")
        output_dir = self.output_dir / str(invoice_id)
        output_dir.mkdir(parents=True, exist_ok=True)

        # FASE 1: PDF → Markdown
        try:
            base_parser = get_parser("unknown")
            markdown = base_parser.parse(pdf_path, output_dir)
            # parse() salva già {stem}.md in output_dir — nessun salvataggio aggiuntivo
            logger.info(f"[Invoice {invoice_id}] Docling completato ({len(markdown):,} caratteri)")
        except Exception as e:
            logger.error(f"[Invoice {invoice_id}] Errore Docling: {e}", exc_info=True)
            return InvoiceStatus(
                id=invoice_id, filename=pdf_path.name,
                status="error", error_field="pdf_parsing", error_message=str(e),
            )

        # FASE 2: Rilevamento carrier
        carrier_name = detect_or_create_carrier(
            markdown=markdown,
            config_dir=self.config_dir,
            ollama_url=self.ollama_base_url,
            ollama_model=self.ollama_model_detect,
        )
        logger.info(f"[Invoice {invoice_id}] Trasportatore identificato: {carrier_name}")

        # Cerca il template (con versione specifica se richiesta)
        carrier_slug = re.sub(r"[^\w]", "_", carrier_name.lower()).strip("_")
        template_filename = f"{carrier_slug}_{template_version}.json" if template_version else f"{carrier_slug}.json"
        template_path = self.config_dir.parent / "templates" / template_filename

        if carrier_name.lower() == "unknown" or not template_path.exists():
            logger.info(
                f"[Invoice {invoice_id}] Carrier '{carrier_name}' senza template. "
                f"Fattura in attesa sample utente."
            )
            pending_markdown_store[invoice_id] = markdown
            pending_carrier_store[invoice_id] = carrier_name
            return InvoiceStatus(id=invoice_id, filename=pdf_path.name, status="awaiting_sample")

        # FASE 3: Estrazione
        return self._extract_and_export(invoice_id, pdf_path, markdown, template_path, output_dir)

    # ------------------------------------------------------------------ #
    #  PIPELINE CON SAMPLE UTENTE                                          #
    # ------------------------------------------------------------------ #

    def process_invoice_with_sample(
        self,
        invoice_id: int,
        pdf_path: Path,
        markdown: str,
        carrier_name: str,
        sample: CarrierSample,
    ) -> InvoiceStatus:
        """
        Genera il template dal campione utente (CarrierSample unificato)
        ed esegue subito l'estrazione sulla fattura.
        Sostituisce process_invoice_with_sample + process_invoice_with_sample_v2.
        """
        logger.info(f"[Invoice {invoice_id}] Avvio sample per '{carrier_name}'.")

        output_dir = self.output_dir / str(invoice_id)
        output_dir.mkdir(parents=True, exist_ok=True)

        carrier_slug = re.sub(r"[^\w]", "_", carrier_name.lower()).strip("_")
        template_path = self.config_dir.parent / "templates" / f"{carrier_slug}.json"

        success = generate_template_from_sample(
            carrier_name=carrier_name,
            markdown_sample=markdown,
            sample=sample,
            output_path=template_path,
            ollama_url=self.ollama_base_url,
            ollama_model=self.ollama_model,
        )

        if not success:
            logger.error(f"[Invoice {invoice_id}] Generazione template fallita.")
            return InvoiceStatus(
                id=invoice_id, filename=pdf_path.name,
                status="error", error_field="template_generation",
                error_message="Impossibile generare il template dal sample fornito.",
            )

        # Arricchisce il YAML con pattern univoci (fingerprint LLM)
        # Costo: 1 chiamata extra, solo al primo sample del carrier
        # Dal secondo upload in poi il carrier viene rilevato senza LLM
        suggested_splitter = enrich_carrier_yaml(
            carrier_name=carrier_name,
            markdown=markdown,
            config_dir=self.config_dir,
            ollama_url=self.ollama_base_url,
            ollama_model=self.ollama_model_detect,
        )
        if suggested_splitter:
            try:
                t = json.loads(template_path.read_text(encoding="utf-8"))
                if t.get("shipment_splitter") == "\n\n":
                    t["shipment_splitter"] = suggested_splitter
                    template_path.write_text(json.dumps(t, indent=2, ensure_ascii=False), encoding="utf-8")
                    logger.info(f"[Invoice {invoice_id}] Splitter aggiornato via LLM: '{suggested_splitter}'")
            except Exception:
                pass
        return self._extract_and_export(invoice_id, pdf_path, markdown, template_path, output_dir)

    # ------------------------------------------------------------------ #
    #  HELPER INTERNO                                                       #
    # ------------------------------------------------------------------ #

    def _extract_and_export(
        self,
        invoice_id: int,
        pdf_path: Path,
        markdown: str,
        template_path: Path,
        output_dir: Path,
    ) -> InvoiceStatus:
        """
        Estrazione con ConfigurableParser + esportazione CSV.
        Estratto in metodo separato per evitare duplicazione tra
        process_invoice e process_invoice_with_sample.
        """
        # Estrazione
        try:
            parser = ConfigurableParser(template_path, self.ollama_base_url, self.ollama_model)
            extraction: InvoiceExtraction = parser.parse_local(markdown, debug_dir=output_dir)
            logger.info(
                f"[Invoice {invoice_id}] Estrazione completata — "
                f"Fattura N: {extraction.invoice_number}, "
                f"Spedizioni: {len(extraction.shipments)}"
            )
        except (ValueError, ValidationError) as e:
            logger.warning(f"[Invoice {invoice_id}] Validazione parziale: {e}")
            return InvoiceStatus(
                id=invoice_id, filename=pdf_path.name,
                status="review_required", error_field="validation", error_message=str(e),
            )
        except Exception as e:
            logger.error(f"[Invoice {invoice_id}] Errore estrazione: {e}", exc_info=True)
            return InvoiceStatus(
                id=invoice_id, filename=pdf_path.name,
                status="error", error_field="extraction", error_message=str(e),
            )

        try:
            template_data = json.loads(template_path.read_text(encoding="utf-8"))
            requested_fields = [f["name"] for f in template_data.get("fields", [])]
        except Exception:
            requested_fields = None

        try:
            csv_path = export_to_csv(extraction, output_dir, invoice_id, requested_fields=requested_fields)
            logger.info(f"[Invoice {invoice_id}] CSV generato: {csv_path.name}")
        except Exception as e:
            logger.error(f"[Invoice {invoice_id}] Errore CSV: {e}", exc_info=True)
            return InvoiceStatus(
                id=invoice_id, filename=pdf_path.name,
                status="error", error_field="csv_export", error_message=str(e),
            )

        return InvoiceStatus(
            id=invoice_id, filename=pdf_path.name,
            status="completed", extraction=extraction,
        )