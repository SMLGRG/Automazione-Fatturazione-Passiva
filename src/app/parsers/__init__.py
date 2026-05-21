# src/app/parsers/__init__.py
import logging
from pathlib import Path
from abc import ABC, abstractmethod

from docling.document_converter import DocumentConverter
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions

logger = logging.getLogger(__name__)


def preprocess_scanned_pdf(pdf_path: Path) -> Path:
    """
    Pre-processing per PDF scansionati di bassa qualità.
    Fase di stabilizzazione successiva (M5): predisposta per deskew e denoise.
    """
    logger.info(f"Pre-processing PDF scansionato: {pdf_path.name}")
    return pdf_path


class BaseParser(ABC):
    """
    Classe base astratta per tutti i parser di fatture trasportatore.
    Gestisce la conversione core da PDF a Markdown tramite Docling.
    """

    def __init__(self):
        from docling.document_converter import PdfFormatOption
        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_ocr = True
        pipeline_options.do_table_structure = True

        self.converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            }
        )

    def convert_to_markdown(self, pdf_path: Path, markdown_output_path: Path) -> str:
        """
        Esegue la conversione fisica del PDF e salva il Markdown intermedio su disco.
        Il salvataggio è fondamentale per l'ispezione visiva e il debug dell'LLM.
        """
        logger.info(f"Avvio conversione Docling per il file: {pdf_path.name}")

        result = self.converter.convert(str(pdf_path))
        markdown_content = result.document.export_to_markdown()

        # Assicura la creazione della cartella di output e scrive il file MD
        markdown_output_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_output_path.write_text(markdown_content, encoding="utf-8")
        logger.info(f"Markdown di debug salvato correttamente in: {markdown_output_path}")

        return markdown_content

    @abstractmethod
    def post_process(self, markdown: str) -> str:
        """
        Logica di pulizia specifica per trasportatore (es. rimozione di intestazioni,
        normalizzazione di tabelle complesse). Implementata dalle classi figlie.
        """
        pass

    def parse(self, pdf_path: Path, output_dir: Path) -> str:
        """
        Punto di ingresso principale del parser: converte il file e applica la pulizia.
        """
        markdown_path = output_dir / f"{pdf_path.stem}.md"
        markdown_raw = self.convert_to_markdown(pdf_path, markdown_path)
        return self.post_process(markdown_raw)


class GenericParser(BaseParser):
    """
    Fallback sicuro: utilizzato quando il trasportatore non è ancora censito.
    Restituisce il testo estratto da Docling senza post-processing distruttivo.
    """

    def post_process(self, markdown: str) -> str:
        return markdown