"""
OllamaExtractor con supporto automatico per documenti lunghi.

Flusso:
  - Se il markdown < MAX_SINGLE_CHUNK_CHARS → estrazione singola (comportamento precedente)
  - Se il markdown ≥ MAX_SINGLE_CHUNK_CHARS → chunked extraction automatica:
      1. Divide in chunk logici (per blocchi di spedizione) o per caratteri
      2. Estrae JSON da ogni chunk
      3. Unisce i risultati deduplicando per tracking_number
      4. Restituisce un unico InvoiceExtraction completo
"""
import json
import logging
import re as _re
import ollama as ollama_client

from app.models import InvoiceExtraction, Shipment

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Prompt                                                              #
# ------------------------------------------------------------------ #

PROMPT_V1_20240601 = """Sei un estrattore di dati specializzato in fatture di trasportatori logistici.
Il tuo compito è estrarre le informazioni strutturate dalla fattura fornita
e restituire ESCLUSIVAMENTE un singolo oggetto JSON valido, senza testo aggiuntivo.

DEFINIZIONI IMPORTANTI:
- MITTENTE (sender_name): chi spedisce la merce, l'azienda emittente (es. "MARCHI & FILDI")
- DESTINATARIO (recipient_name): chi riceve la merce, il cliente finale
- DESTINAZIONE (destination_city): la città del destinatario, NON del mittente
- CAP (destination_postal_code): il codice postale del destinatario, NON del mittente
- PAESE (destination_country): il paese del destinatario, NON del mittente
- DATA SPEDIZIONE (shipment_date): la data della singola spedizione dopo "Del", NON la data della fattura
- PESO (weight_kg): il valore numerico dopo la parola "Lordi" nel testo
- PREZZO (total_amount): il valore dopo "Totale EUR" in fondo al blocco spedizione, NON le singole voci di dettaglio

FORMATO TIPICO DI UN BLOCCO SPEDIZIONE (Galardi):
  Spedizione :ETMP010 303032 Del 10/01/23
  Mittente :MARCHI & FILDI   Destinatario :ABSOLUTE FRESH UNIPESS
   :13900 BIELLA IT          :3754 905 VALONGO DA VOUGA PT
  Lordi 1348,10
  Totale EUR 373,85

In questo formato la riga ":13900 BIELLA IT" è mittente (ignora),
la riga ":3754 905 VALONGO DA VOUGA PT" contiene: CAP=3754, città=VALONGO DA VOUGA, paese=PT

ESEMPIO COMPLETO:

Input:
CARRIER: Galardi
FATTURA: 2300800441 del 31/01/2023
TOTALE FATTURA: 5729.11
Spedizione :ETMP010 303032 Del 10/01/23
Mittente :MARCHI & FILDI   Destinatario :ABSOLUTE FRESH UNIPESS
 :13900 BIELLA IT          :3754 905 VALONGO DA VOUGA PT
Lordi 1348,10
Totale EUR 373,85

Output JSON:
{{
  "carrier_name": "Galardi",
  "invoice_number": "2300800441",
  "invoice_date": "2023-01-31",
  "total_invoice_amount": "5729.11",
  "shipments": [
    {{
      "tracking_number": "ETMP010 303032",
      "shipment_date": "2023-01-10",
      "sender_name": "MARCHI & FILDI",
      "recipient_name": "ABSOLUTE FRESH UNIPESS",
      "destination_city": "VALONGO DA VOUGA",
      "destination_postal_code": "3754",
      "destination_country": "PT",
      "weight_kg": "1348.10",
      "service_type": "CIP",
      "total_amount": "373.85"
    }}
  ]
}}

REGOLE OBBLIGATORIE:
- Restituisci UN SOLO oggetto JSON con shipments annidati dentro
- Nomi campo esatti: carrier_name, invoice_number, invoice_date, total_invoice_amount, shipments
- Per ogni spedizione: tracking_number, shipment_date, sender_name, recipient_name, destination_city, destination_postal_code, destination_country, weight_kg, service_type, total_amount
- Date in formato YYYY-MM-DD
- Importi e pesi come stringa numerica con punto decimale (es. "1348.10" non "1348,10")
- total_amount è SEMPRE il valore dopo "Totale EUR" in fondo al blocco, mai le singole voci (005, 065, B23, 113)
- destination_country è il paese del DESTINATARIO (codice a 2 lettere dopo la città del destinatario)
- destination_postal_code è il codice numerico prima della città del destinatario
- weight_kg è il numero dopo "Lordi" nel blocco spedizione
- service_type è il codice resa tra parentesi es. CIP, DAP, C&F (non "C&amp;F")
- Se un campo non è presente nel testo, usa null

--- FATTURA DA PROCESSARE ---
{markdown}"""

ACTIVE_PROMPT = PROMPT_V1_20240601

# ------------------------------------------------------------------ #
#  Costanti                                                            #
# ------------------------------------------------------------------ #

# Soglia oltre la quale si attiva la chunked extraction
MAX_SINGLE_CHUNK_CHARS = 9_000

# Dimensione di ogni chunk (stare abbondantemente sotto il context 8K del modello)
CHUNK_SIZE_CHARS = 7_500

# Sovrapposizione tra chunk consecutivi (evita di perdere dati a cavallo)
CHUNK_OVERLAP_CHARS = 500

# Pattern che segnalano l'inizio di un nuovo blocco spedizione.
# Vengono usati per spezzare il documento in punti logici anziché a caso.
_BLOCK_START_PATTERNS = [
    r"(?=Groupage\s*:)",           # Galardi
    r"(?=Spedizione\s*:)",         # Galardi
    r"(?=\bTracking\b)",           # generico EN
    r"(?=\bShipment\b)",           # generico EN
    r"(?=\bConsignment\b)",        # generico EN
    r"(?=AWB[:\s])",               # Air Waybill
    r"(?=\bWaybill\b)",
    r"(?=\bBill\s+of\s+Lading\b)",
    r"(?=\bB/L\b)",
]
_BLOCK_START_RE = _re.compile(
    "|".join(_BLOCK_START_PATTERNS),
    _re.IGNORECASE,
)


# ------------------------------------------------------------------ #
#  OllamaExtractor                                                     #
# ------------------------------------------------------------------ #

class OllamaExtractor:
    """
    Estrae dati strutturati da markdown di fattura tramite Ollama.
    Gestisce automaticamente documenti lunghi tramite chunked extraction.
    """

    def __init__(self, base_url: str, model_name: str):
        self.base_url = base_url
        self.model_name = model_name
        self._client = ollama_client.Client(host=base_url)

    # ---------------------------------------------------------------- #
    #  API pubblica                                                      #
    # ---------------------------------------------------------------- #

    def extract(self, markdown: str) -> InvoiceExtraction:
        """
        Estrae i dati dalla fattura in markdown.
        Se il documento è lungo, usa la chunked extraction automaticamente.
        """
        if len(markdown) <= MAX_SINGLE_CHUNK_CHARS:
            logger.info("Estrazione diretta (documento entro i limiti).")
            return self._extract_single(markdown)

        logger.info(
            f"Documento lungo ({len(markdown):,} chars > {MAX_SINGLE_CHUNK_CHARS:,}): "
            "attivata chunked extraction."
        )
        return self._extract_chunked(markdown)

    # ---------------------------------------------------------------- #
    #  Estrazione singola                                                #
    # ---------------------------------------------------------------- #

    def _extract_single(self, markdown: str) -> InvoiceExtraction:
        """Estrazione su chunk singolo (il comportamento originale)."""
        prompt = ACTIVE_PROMPT.format(markdown=markdown)

        try:
            response = self._client.chat(
                model=self.model_name,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Sei un estrattore di dati. "
                            "Rispondi ESCLUSIVAMENTE con un singolo oggetto JSON valido. "
                            "Nessun testo aggiuntivo, nessuna spiegazione, nessun markdown."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                options={"num_ctx": 8192, "num_predict": 2048},
            )

            raw_json = response.message.content
            logger.debug(f"Risposta grezza ({len(raw_json)} chars): {raw_json[:300]}")

            raw_json = self._clean_raw_json(raw_json)
            data = json.loads(raw_json)
            extraction = InvoiceExtraction.model_validate(data)

            logger.info(
                f"Estrazione completata: fattura {extraction.invoice_number}, "
                f"{len(extraction.shipments)} spedizioni"
            )
            return extraction

        except ConnectionError as e:
            raise RuntimeError(f"Ollama non raggiungibile: {self.base_url}") from e
        except json.JSONDecodeError as e:
            raise ValueError(f"Risposta Ollama non parsabile come JSON: {e}") from e
        except Exception:
            raise

    # ---------------------------------------------------------------- #
    #  Chunked extraction                                                #
    # ---------------------------------------------------------------- #

    def _extract_chunked(self, markdown: str) -> InvoiceExtraction:
        """
        Divide il markdown in chunk logici, estrae da ciascuno,
        poi unisce i risultati.
        """
        chunks = self._build_chunks(markdown)
        logger.info(f"Chunked extraction: {len(chunks)} chunk da elaborare.")

        header_extraction: InvoiceExtraction | None = None
        all_shipments: list[Shipment] = []
        seen_tracking: set[str] = set()

        for idx, chunk in enumerate(chunks, 1):
            logger.info(f"  → Chunk {idx}/{len(chunks)} ({len(chunk):,} chars)...")
            try:
                result = self._extract_single(chunk)

                # Il primo chunk valido fornisce i metadati della fattura
                if header_extraction is None:
                    header_extraction = result
                    # NON azzerare le spedizioni: il primo chunk le contiene già
                    # le aggiungiamo nel loop sottostante come tutti gli altri

                # Aggiungi spedizioni deduplicando per tracking_number
                for shipment in result.shipments:
                    key = (
                        shipment.tracking_number.strip()
                        if shipment.tracking_number
                        else f"__notrack_{idx}_{len(all_shipments)}"
                    )
                    if key not in seen_tracking:
                        seen_tracking.add(key)
                        all_shipments.append(shipment)

                logger.info(
                    f"    Chunk {idx}: {len(result.shipments)} spedizioni "
                    f"({len(all_shipments)} totale univoche finora)"
                )

            except Exception as e:
                logger.warning(f"    Chunk {idx} fallito: {e} — continuo con il prossimo.")

        if header_extraction is None:
            raise RuntimeError(
                "Nessun chunk ha prodotto un'estrazione valida. "
                "Verificare il modello Ollama e il contenuto del markdown."
            )

        header_extraction.shipments = all_shipments
        logger.info(
            f"Chunked extraction completata: fattura {header_extraction.invoice_number}, "
            f"{len(all_shipments)} spedizioni univoche totali."
        )
        return header_extraction

    # ---------------------------------------------------------------- #
    #  Costruzione chunk                                                 #
    # ---------------------------------------------------------------- #

    def _build_chunks(self, markdown: str) -> list[str]:
        """
        Costruisce i chunk tentando prima di spezzare il documento nei punti
        logici (inizio blocco spedizione). Se non trova pattern, usa il
        chunking per caratteri con sovrapposizione.
        """
        # Prova split per blocchi logici
        blocks = _BLOCK_START_RE.split(markdown)
        blocks = [b.strip() for b in blocks if b.strip()]

        if len(blocks) > 1:
            logger.debug(f"Split logico: {len(blocks)} blocchi trovati.")
            return self._group_blocks_into_chunks(blocks)

        # Fallback: split per caratteri
        logger.debug("Nessun pattern di blocco trovato: uso split per caratteri.")
        return self._char_chunks(markdown)

    def _group_blocks_into_chunks(self, blocks: list[str]) -> list[str]:
        """
        Raggruppa i blocchi logici in chunk che non superino CHUNK_SIZE_CHARS.
        Il primo blocco (intestazione fattura) viene preposto a ogni chunk
        per dare contesto all'LLM (numero fattura, carrier, data).
        """
        if not blocks:
            return []

        header_block = blocks[0]
        chunks: list[str] = []
        current_parts: list[str] = [header_block]
        current_len = len(header_block)

        for block in blocks[1:]:
            if current_len + len(block) + 1 <= CHUNK_SIZE_CHARS:
                current_parts.append(block)
                current_len += len(block) + 1
            else:
                chunks.append("\n".join(current_parts))
                # Ogni nuovo chunk riparte dall'header per avere il contesto fattura
                current_parts = [header_block, block]
                current_len = len(header_block) + len(block) + 1

        if current_parts:
            chunks.append("\n".join(current_parts))

        return chunks

    def _char_chunks(self, markdown: str) -> list[str]:
        """
        Chunking a caratteri con sovrapposizione.
        Cerca di spezzare all'ultimo a-capo per non tagliare a metà una riga.
        """
        chunks: list[str] = []
        start = 0
        total = len(markdown)

        while start < total:
            end = min(start + CHUNK_SIZE_CHARS, total)

            # Spezza all'ultimo a-capo per non troncare dati
            if end < total:
                nl = markdown.rfind("\n", start, end)
                if nl > start + CHUNK_SIZE_CHARS // 2:
                    end = nl

            chunks.append(markdown[start:end])
            # Il prossimo chunk riparte OVERLAP_CHARS prima della fine attuale
            start = max(start + 1, end - CHUNK_OVERLAP_CHARS)

        return chunks

    # ---------------------------------------------------------------- #
    #  Pulizia JSON grezzo                                               #
    # ---------------------------------------------------------------- #

    def _clean_raw_json(self, raw: str) -> str:
        """
        Normalizza la risposta grezza del modello:
        1. Rimuove fence markdown ```json ... ```
        2. Ricompone JSON spezzato (header object + shipments array separati)
        3. Normalizza numeri in formato italiano (1.234,56 → 1234.56)
        """
        # 1. Rimuove fence markdown
        if "```" in raw:
            raw = raw.split("```json")[-1].split("```")[0].strip()

        # 2. Ricompone JSON spezzato: {header} seguito da [array]
        obj_match = _re.search(r"\{.*?\}(?=\s*\[)", raw, _re.DOTALL)
        arr_match = _re.search(r"\[.*\]", raw, _re.DOTALL)
        if obj_match and arr_match:
            try:
                header = json.loads(obj_match.group())
                shipments = json.loads(arr_match.group())
                header["shipments"] = shipments
                raw = json.dumps(header)
                logger.debug("JSON spezzato ricomposto automaticamente.")
            except Exception:
                pass

        # 3. Normalizza numeri in formato italiano nei valori stringa
        def normalize_number(m: _re.Match) -> str:
            val = m.group(1)
            if _re.match(r"^\d{1,3}(\.\d{3})+,\d{2}$", val):
                val = val.replace(".", "").replace(",", ".")
            elif _re.match(r"^\d+,\d{2}$", val):
                val = val.replace(",", ".")
            return f'"{val}"'

        raw = _re.sub(r'"([\d.,]+)"', normalize_number, raw)
        return raw