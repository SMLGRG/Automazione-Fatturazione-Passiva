# src/app/parsers/__init__.py
"""
Parser base per la conversione PDF → Markdown.

Novità rispetto alla versione precedente:
  - BaseParser.parse() chiama SmartCompressor dopo post_process(),
    eliminando automaticamente header/footer ripetuti su ogni pagina.
  - Funziona per qualsiasi carrier senza configurazione manuale aggiuntiva.
  - GalardiParser mantiene la sua logica regex specifica (che produce già
    un output molto compatto), quindi SmartCompressor su di esso fa poco
    ma non è dannoso.
"""
import logging
from pathlib import Path
from abc import ABC, abstractmethod

from docling.document_converter import DocumentConverter
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions

from app.services.smart_compressor import SmartCompressor

logger = logging.getLogger(__name__)

# Istanza condivisa del compressore (stateless, sicura per uso concorrente)
_compressor = SmartCompressor(repeat_threshold=0.55, min_line_len=10)


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
    Gestisce la conversione core da PDF a Markdown tramite Docling e
    la compressione automatica tramite SmartCompressor.
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
        Esegue la conversione fisica del PDF e salva il Markdown grezzo su disco.
        Il salvataggio è fondamentale per l'ispezione visiva e il debug dell'LLM.
        """
        logger.info(f"Avvio conversione Docling per il file: {pdf_path.name}")

        result = self.converter.convert(str(pdf_path))
        markdown_content = result.document.export_to_markdown()

        markdown_output_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_output_path.write_text(markdown_content, encoding="utf-8")
        logger.info(
            f"Markdown grezzo salvato in: {markdown_output_path} "
            f"({len(markdown_content):,} chars)"
        )
        return markdown_content

    def _clean_markdown(self, markdown: str) -> str:
        """
        Rimuove rumore dal Markdown prima di mandarlo all'LLM:
        - tag immagine inutili
        - righe vuote multiple
        - intestazioni ripetute di pagina
        """
        import re
        markdown = re.sub(r"<!--\s*image\s*-->", "", markdown)
        # Decodifica entità HTML lasciate da Docling (es. C&amp;F → C&F)
        import html
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
        Pipeline completa: PDF → Markdown grezzo → pulizia base →
        post-processing specifico per carrier → SmartCompressor.

        Il markdown finale è già ottimizzato per la lunghezza del contesto LLM.
        """
        # 1. PDF → Markdown grezzo (salvato su disco per debug)
        markdown_path = output_dir / f"{pdf_path.stem}.md"
        markdown_raw = self.convert_to_markdown(pdf_path, markdown_path)

        # 2. Pulizia base (tag immagine, righe vuote)
        markdown_clean = self._clean_markdown(markdown_raw)

        # 3. Post-processing specifico per carrier
        markdown_processed = self.post_process(markdown_clean)

        # 4. SmartCompressor: rimozione automatica header/footer ripetuti
        #    Questo step è il cuore della novità: funziona per qualsiasi carrier
        #    senza configurazione manuale, analizzando la frequenza delle righe
        #    nelle diverse pagine del documento.
        markdown_final = _compressor.compress(markdown_processed)

        # 5. Salva la versione compressa su disco (utile per il debug dell'LLM)
        compressed_path = output_dir / f"{pdf_path.stem}_compressed.md"
        compressed_path.write_text(markdown_final, encoding="utf-8")
        logger.info(
            f"Markdown compresso salvato in: {compressed_path} "
            f"({len(markdown_final):,} chars)"
        )

        return markdown_final


class GenericParser(BaseParser):
    """
    Fallback sicuro: utilizzato quando il trasportatore non è ancora censito.

    Con l'aggiunta di SmartCompressor nel metodo parse() della classe base,
    anche il GenericParser ora produce output compresso e adatto all'LLM
    senza alcuna configurazione manuale aggiuntiva.
    """

    def post_process(self, markdown: str) -> str:
        # Nessuna logica regex specifica: restituisce il testo così com'è.
        # La compressione avviene nel passo successivo (SmartCompressor).
        return markdown