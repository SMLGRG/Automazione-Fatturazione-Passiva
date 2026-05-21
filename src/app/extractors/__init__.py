# src/app/extractors/__init__.py
import json
import logging
import ollama as ollama_client

from app.models import InvoiceExtraction

logger = logging.getLogger(__name__)

PROMPT_V1_20240601 = """Sei un estrattore di dati specializzato in fatture di trasportatori logistici.
Il tuo compito è estrarre le informazioni strutturate dalla fattura fornita in formato Markdown
e restituire ESCLUSIVAMENTE un oggetto JSON valido, senza testo aggiuntivo.

ESEMPI:

--- ESEMPIO 1 ---
Input (Markdown parziale):
## Fattura DHL Express
Numero: 2024-00123 | Data: 15/03/2024

| Tracking | Destinatario | Paese | Peso | Importo |
|---|---|---|---|---|
| 1Z999AA10123456784 | Mario Rossi | IT | 2.5 kg | 12,50 € |

**Totale fattura: 12,50 €**

Output JSON:
{{
  "carrier_name": "DHL Express",
  "invoice_number": "2024-00123",
  "invoice_date": "2024-03-15",
  "total_invoice_amount": "12.50",
  "shipments": [
    {{
      "tracking_number": "1Z999AA10123456784",
      "invoice_date": "2024-03-15",
      "total_amount": "12.50",
      "recipient_name": "Mario Rossi",
      "destination_country": "IT",
      "weight_kg": "2.5",
      "service_type": "EXPRESS"
    }}
  ]
}}

--- FATTURA DA PROCESSARE ---
{markdown}"""

ACTIVE_PROMPT = PROMPT_V1_20240601


class OllamaExtractor:
    """
    Estrae dati strutturati dal Markdown di una fattura usando phi4-mini via Ollama.
    Usa il structured output nativo di Ollama (parametro format) al posto di Outlines,
    che garantisce lo stesso vincolo grammaticale con API stabili.
    """

    def __init__(self, base_url: str, model_name: str):
        self.base_url = base_url
        self.model_name = model_name
        self._client = ollama_client.Client(host=base_url)

    def _build_prompt(self, markdown: str) -> str:
        return ACTIVE_PROMPT.format(markdown=markdown)

    def extract(self, markdown: str) -> InvoiceExtraction:
        prompt = self._build_prompt(markdown)
        schema = InvoiceExtraction.model_json_schema()

        try:
            response = self._client.chat(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                format=schema,
            )
            raw_json = response.message.content
            logger.debug(f"Risposta grezza Ollama: {raw_json[:200]}...")

            data = json.loads(raw_json)
            extraction = InvoiceExtraction.model_validate(data)

            logger.info(
                f"Estrazione completata: fattura {extraction.invoice_number}, "
                f"{len(extraction.shipments)} spedizioni"
            )
            return extraction

        except ConnectionError as e:
            logger.error(f"Impossibile connettersi a Ollama su {self.base_url}: {e}")
            raise RuntimeError(f"Ollama non raggiungibile: {self.base_url}") from e

        except json.JSONDecodeError as e:
            logger.error(f"JSON non valido ricevuto da Ollama: {e}")
            raise ValueError(f"Risposta Ollama non parsabile come JSON: {e}") from e

        except Exception as e:
            logger.error(f"Errore durante l'estrazione: {e}", exc_info=True)
            raise