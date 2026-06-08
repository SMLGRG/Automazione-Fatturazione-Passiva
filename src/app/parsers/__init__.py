# src/app/parsers/__init__.py
import logging
from pathlib import Path
from abc import ABC, abstractmethod

from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions

from app.models import InvoiceExtraction

logger = logging.getLogger(__name__)



class BaseParser(ABC):
    """
    Classe base astratta per tutti i parser di fatture trasportatore.
    Gestisce la conversione core da PDF a Markdown tramite Docling e
    la compressione automatica tramite SmartCompressor.
    """

    def __init__(self):
        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_ocr = False
        pipeline_options.do_table_structure = True

        self.converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            }
        )

    def convert_to_markdown(self, pdf_path: Path, markdown_output_path: Path) -> str:
        """
        Esegue la conversione fisica del PDF e salva il Markdown grezzo su disco.
        Il salvataggio è fondamentale per l'ispezione visiva e il debug dell'LLM.
        """
        logger.info(f"Avvio conversione Docling per il file: {pdf_path.name}")

        result = self.converter.convert(str(pdf_path))
        markdown_content = result.document.export_to_markdown()

        markdown_output_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_output_path.write_text(markdown_content, encoding="utf-8")
        logger.info(f"Markdown grezzo salvato in: {markdown_output_path} ({len(markdown_content):,} chars)")
        return markdown_content

    def _clean_markdown(self, markdown: str) -> str:
        """
        Rimuove rumore dal Markdown prima di mandarlo all'LLM/Parser:
        - tag immagine inutili, righe vuote multiple, entità HTML non decodificate
        """
        import re
        import html
        markdown = re.sub(r"<!--.*?-->", "", markdown, flags=re.DOTALL)
        markdown = html.unescape(markdown)
        markdown = re.sub(r"Pag\.\s*\d+\s*di\s*\d+", "", markdown)
        markdown = re.sub(r"\n{3,}", "\n\n", markdown)
        return markdown.strip()

    @abstractmethod
    def post_process(self, markdown: str) -> str:
        """
        Logica di pulizia specifica per trasportatore.
        Implementata dalle classi figlie.
        """
        pass

    def parse(self, pdf_path: Path, output_dir: Path) -> str:
        """
        Pipeline: PDF → Markdown grezzo → pulizia base → post-processing.
        SmartCompressor rimosso: il chunking nel parser gestisce la lunghezza.
        """
        markdown_path = output_dir / f"{pdf_path.stem}.md"
        markdown_raw = self.convert_to_markdown(pdf_path, markdown_path)
        markdown_clean = self._clean_markdown(markdown_raw)
        markdown_final = self.post_process(markdown_clean)

        return markdown_final

    def has_regex_rules(self) -> bool:
        """Ritorna True se il parser possiede un motore regex locale pronto ad estrarre i dati."""
        return False

    def parse_local(self, markdown: str) -> InvoiceExtraction:
        """Esegue il parsing locale deterministico basato su Regex. Sovrascritto nei parser configurabili."""
        raise NotImplementedError("Questo parser non supporta l'estrazione locale deterministica.")


class GenericParser(BaseParser):
    """
    Fallback sicuro: utilizzato quando il trasportatore non è ancora censito o
    quando si esegue la prima conversione grezza del testo.
    """
    def post_process(self, markdown: str) -> str:
        return markdown