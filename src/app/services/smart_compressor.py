# app/services/smart_compressor.py
"""
SmartCompressor — compressione automatica di markdown multi-pagina.

Problema: Docling converte un PDF da 9 pagine in ~40.000 caratteri di markdown.
Logo, intestazione, indirizzi sedi e disclaimer legali si ripetono su ogni pagina.
Questo modulo li rimuove automaticamente, senza configurazione manuale, per
qualsiasi layout di fattura.

Algoritmo:
  1. Divide il documento nelle singole pagine (marker "Pag. 1 di 9", ecc.)
  2. Rileva le righe che compaiono in ≥ repeat_threshold delle pagine
  3. Applica safeguard: le righe con importi monetari o tracking number non
     vengono mai rimosse (potrebbero essere dati ripetuti per coincidenza)
  4. Mantiene la PRIMA pagina intatta (contiene header fattura: numero, data,
     carrier — info necessarie all'LLM per costruire il JSON)
  5. Rimuove il boilerplate dalle pagine successive

Riduzione tipica su fattura 9 pagine: da ~38.000 a ~8.000 chars (≈ -79%).
"""
import re
import logging
from collections import Counter

logger = logging.getLogger(__name__)

# Marker di cambio pagina supportati
_PAGE_BREAK_PATTERNS = [
    r"Pag\.\s*\d+\s*di\s*\d+",
    r"Page\s*\d+\s*of\s*\d+",
    r"Página\s*\d+\s*de\s*\d+",
    r"Seite\s*\d+\s*von\s*\d+",
    r"Pagina\s*\d+\s*di\s*\d+",
]
_PAGE_BREAK_RE = re.compile(
    "|".join(_PAGE_BREAK_PATTERNS),
    re.IGNORECASE | re.MULTILINE,
)

# Righe con questi pattern non vengono mai classificate come boilerplate,
# anche se compaiono su più pagine. Proteggono importi e tracking number
# che per coincidenza si ripetono su pagine diverse.
_PROTECTED_PATTERNS = [
    re.compile(r"\d+[.,]\d{2}"),          # importi monetari: 373,85 / 104.96
    re.compile(r"\b[A-Z]{2,}\d{5,}\b"),   # tracking number stile ETMP010303032
    re.compile(r"\bTotale\b", re.I),       # righe "Totale EUR ..."
    re.compile(r"\bTotal\b", re.I),
    re.compile(r"\bImport[eo]\b", re.I),
    re.compile(r"\bAmount\b", re.I),
]


def _is_protected(line: str) -> bool:
    """Restituisce True se la riga contiene dati che non vanno mai rimossi."""
    return any(p.search(line) for p in _PROTECTED_PATTERNS)


class SmartCompressor:
    """
    Compressore intelligente di markdown per fatture multi-pagina.

    Parametri:
        repeat_threshold: Percentuale di pagine in cui una riga deve comparire
                          per essere classificata come boilerplate (default 0.60).
        min_line_len:     Lunghezza minima di una riga perché venga analizzata.
    """

    def __init__(
        self,
        repeat_threshold: float = 0.60,
        min_line_len: int = 10,
    ):
        self.repeat_threshold = repeat_threshold
        self.min_line_len = min_line_len

    # ------------------------------------------------------------------
    # API pubblica
    # ------------------------------------------------------------------

    def compress(self, markdown: str) -> str:
        """
        Comprime il markdown, mantiene la prima pagina intatta,
        rimuove boilerplate dalle pagine successive.
        """
        original_len = len(markdown)

        pages = self._split_pages(markdown)
        n_pages = len(pages)

        if n_pages < 2:
            logger.debug("SmartCompressor: documento a pagina singola, solo pulizia base.")
            return self._clean_noise(markdown)

        # Rileva boilerplate
        boilerplate = self._detect_boilerplate(pages, n_pages)
        logger.debug(
            f"SmartCompressor: {len(boilerplate)} righe boilerplate "
            f"su {n_pages} pagine."
        )

        # Prima pagina: sempre intatta (header fattura)
        first_page = self._clean_noise(pages[0])

        # Pagine successive: filtrate
        rest_pages = []
        for page in pages[1:]:
            filtered = self._remove_boilerplate(page, boilerplate)
            cleaned = self._clean_noise(filtered)
            if cleaned:
                rest_pages.append(cleaned)

        compressed = "\n\n".join([first_page] + rest_pages)
        compressed = re.sub(r"\n{3,}", "\n\n", compressed).strip()

        reduction = 1 - len(compressed) / original_len if original_len else 0
        logger.info(
            f"SmartCompressor: {original_len:,} → {len(compressed):,} chars "
            f"({reduction:.0%} riduzione, {n_pages} pagine, "
            f"{len(boilerplate)} righe boilerplate)"
        )
        return compressed

    # ------------------------------------------------------------------
    # Privati: suddivisione in pagine
    # ------------------------------------------------------------------

    def _split_pages(self, markdown: str) -> list[str]:
        parts = _PAGE_BREAK_RE.split(markdown)
        pages = [p.strip() for p in parts if p.strip()]
        return pages if len(pages) > 1 else [markdown]

    # ------------------------------------------------------------------
    # Privati: rilevamento boilerplate
    # ------------------------------------------------------------------

    def _normalize(self, line: str) -> str:
        """Normalizzazione leggera: lowercase + collasso spazi."""
        return re.sub(r"\s{2,}", " ", line.strip()).lower()

    def _detect_boilerplate(self, pages: list[str], n_pages: int) -> set[str]:
        """
        Classifica come boilerplate le righe che:
        - Compaiono in ≥ repeat_threshold delle pagine
        - NON contengono importi monetari o tracking number (_is_protected)
        """
        freq: Counter = Counter()

        for page in pages:
            seen: set[str] = set()
            for raw_line in page.splitlines():
                stripped = raw_line.strip()
                if len(stripped) < self.min_line_len:
                    continue
                # Non analizzare righe protette: non saranno mai rimosse
                if _is_protected(stripped):
                    continue
                norm = self._normalize(stripped)
                if norm and norm not in seen:
                    freq[norm] += 1
                    seen.add(norm)

        min_count = max(2, n_pages * self.repeat_threshold)
        return {norm for norm, count in freq.items() if count >= min_count}

    # ------------------------------------------------------------------
    # Privati: rimozione e pulizia
    # ------------------------------------------------------------------

    def _remove_boilerplate(self, page_text: str, boilerplate: set[str]) -> str:
        if not boilerplate:
            return page_text

        filtered: list[str] = []
        for raw_line in page_text.splitlines():
            stripped = raw_line.strip()

            # Righe corte: mantieni sempre
            if len(stripped) < self.min_line_len:
                filtered.append(raw_line)
                continue

            # Righe protette (importi, tracking): mai rimuovere
            if _is_protected(stripped):
                filtered.append(raw_line)
                continue

            # Rimuovi solo se boilerplate confermato
            norm = self._normalize(stripped)
            if norm not in boilerplate:
                filtered.append(raw_line)

        return "\n".join(filtered)

    def _clean_noise(self, text: str) -> str:
        """Pulizia base: tag immagine, separatori grafici, marker pagina."""
        text = re.sub(r"<!--\s*image\s*-->", "", text)
        text = re.sub(r"^\s*[=\-_*]{4,}\s*$", "", text, flags=re.MULTILINE)
        text = _PAGE_BREAK_RE.sub("", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()