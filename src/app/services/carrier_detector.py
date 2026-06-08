# src/app/services/carrier_detector.py
"""
Identificazione automatica del corriere da una fattura in formato Markdown.

Flusso:
1. Match veloce su keyword dai YAML esistenti (zero costo).
2. Match regex su patterns dai YAML esistenti (utile quando il logo è un'immagine).
3. Se fallisce, chiama l'LLM con solo l'header del documento (< 1500 chars).
4. Crea automaticamente il YAML strutturato per il futuro.
"""
import re
import logging
import httpx
import json
import yaml
from pathlib import Path

logger = logging.getLogger(__name__)

_PROMPT_IDENTIFICA_CORRIERE = """Stai leggendo l'intestazione di una fattura di trasporto/logistica.
Identifica il nome del corriere o spedizioniere che ha EMESSO la fattura (non il cliente).
Rispondi con UNA SOLA PAROLA in maiuscolo, senza spazi, senza punteggiatura.
Esempi validi: EUROTIR, ALBINI, GALARDI, BRT, FEDEX, CARGONORD, DHL
Se proprio non riesci a identificarlo, rispondi: UNKNOWN"""


def _extract_keywords_from_header(header: str, carrier_name: str) -> list[str]:
    """Estrae keyword utili dall'header per popolare il YAML."""
    keywords = set()
    keywords.add(carrier_name.upper())
    keywords.add(carrier_name.lower())

    # Domini web/email (es. "eurotirsrl" da "eurotirsrl.com")
    domains = re.findall(r"([\w\-]+)\.\w{2,4}", header)
    for d in domains:
        clean = d.lower().strip()
        if len(clean) > 4 and clean not in {"mail", "http", "www", "info", "fax", "tel"}:
            keywords.add(clean)

    # Varianti del nome carrier nel testo
    name_lower = carrier_name.lower()
    for line in header.splitlines():
        if name_lower in line.lower():
            matches = re.findall(rf"\b{re.escape(carrier_name)}\b", line, re.IGNORECASE)
            keywords.update(matches)

    return list(set(k for k in keywords if len(k) >= 4 and not k.isdigit()))


def _extract_auto_patterns(header: str) -> list[str]:
    """
    Genera pattern regex automaticamente dall'header per rilevamento robusto.
    Cerca codici alfanumerici univoci che identificano il carrier anche
    quando il nome non è nel testo (es. logo come immagine).
    Es: "ETMP010" da Galardi, "CCIAA137945MN" da EUROTIR.
    """
    patterns = []
    # Codici alfanumerici misti (es. ETMP010, 3G0550, CCIAA137945MN)
    codes = re.findall(r"\b[A-Z]{2,}[0-9]{2,}[A-Z0-9]*\b", header)
    seen = set()
    for code in codes:
        if len(code) >= 5 and code not in seen:
            patterns.append(re.escape(code))
            seen.add(code)
        if len(patterns) >= 4:
            break
    return patterns


def _create_yaml(carrier_name: str, header: str, config_dir: Path) -> Path:
    """
    Crea il file YAML strutturato del corriere con keyword e pattern estratti.
    """
    keywords = _extract_keywords_from_header(header, carrier_name)
    patterns = _extract_auto_patterns(header)
    carrier_slug = re.sub(r"[^\w]", "_", carrier_name.lower()).strip("_")

    yaml_data = {
        "carrier_name": carrier_name.upper(),
        "version": 1,
        # Parole chiave per match veloce nel testo
        "keywords": sorted(keywords),
        # Pattern regex per match robusto (utile quando il logo è un'immagine)
        "patterns": patterns,
        # Alias nomi colonne → campi standard del modello (per normalizzazione futura)
        "column_aliases": {
            "tracking_number": [],
            "total_amount": [],
            "weight_kg": [],
        },
        # Documentazione layout (opzionale, compilare manualmente)
        "layout_doc": f"docs/layouts/{carrier_slug}_invoice_layout.md",
    }

    config_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = config_dir / f"{carrier_slug}.yaml"

    with open(yaml_path, "w", encoding="utf-8") as f:
        # Header commento
        f.write(f"# src/config/carriers/{carrier_slug}.yaml\n")
        f.write(f"# Generato automaticamente — arricchire keywords, patterns e column_aliases manualmente\n\n")
        yaml.dump(yaml_data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    logger.info(f"[CarrierDetector] YAML creato: {yaml_path.name} | keywords: {keywords} | patterns: {patterns}")
    return yaml_path


def _llm_identify_carrier(header: str, ollama_url: str, ollama_model: str) -> str:
    """Chiama l'LLM per identificare il corriere dall'header. Restituisce nome in maiuscolo o 'UNKNOWN'."""
    try:
        logger.info(f"[CarrierDetector] Identificazione corriere via LLM ({ollama_model})...")
        response = httpx.post(
            f"{ollama_url.rstrip('/')}/api/chat",
            json={
                "model": ollama_model,
                "messages": [
                    {"role": "system", "content": _PROMPT_IDENTIFICA_CORRIERE},
                    {"role": "user", "content": f"Intestazione fattura:\n\n{header}"},
                ],
                "options": {"temperature": 0.0},
                "stream": False,
            },
            timeout=60.0,
        )
        response.raise_for_status()
        raw = response.json()["message"]["content"].strip()
        carrier = re.sub(r"[^\w]", "", raw.split()[0]).upper() if raw.split() else "UNKNOWN"
        logger.info(f"[CarrierDetector] LLM ha identificato: '{carrier}'")
        return carrier
    except Exception as e:
        logger.error(f"[CarrierDetector] Errore LLM: {e}")
        return "UNKNOWN"

_PROMPT_FINGERPRINT = """Stai analizzando il testo di una fattura di trasporto convertita da PDF.
Il nome del corriere potrebbe essere assente dal testo (solo nel logo immagine).

Trova nel testo delle stringhe UNIVOCHE che identificano questo corriere specifico:
- Codici interni (es. "EESMAUPER", "FATC ST", "ETMP010")
- Stringhe ripetute che non siano dati della spedizione
- Qualsiasi testo che compaia solo nelle fatture di QUESTO corriere

Trova anche il separatore tra spedizioni: una stringa che si ripete prima di ogni spedizione.

Rispondi SOLO con questo JSON:
{
  "unique_strings": ["stringa1", "stringa2"],
  "suggested_splitter": "la stringa separatore"
}
Solo il JSON, nessun testo aggiuntivo."""


def _llm_fingerprint_carrier(
    markdown: str,
    ollama_url: str,
    ollama_model: str,
) -> dict:
    """
    Analizza il markdown completo per trovare stringhe univoche del carrier
    e lo splitter suggerito. Chiamato UNA SOLA VOLTA durante il primo sample.
    Risultato salvato nel YAML come patterns → usato da STEP 2 nei run successivi.
    """
    try:
        response = httpx.post(
            f"{ollama_url.rstrip('/')}/api/chat",
            json={
                "model": ollama_model,
                "messages": [
                    {"role": "system", "content": _PROMPT_FINGERPRINT},
                    {"role": "user", "content": f"Testo fattura:\n\n{markdown[:3000]}"},
                ],
                "options": {"temperature": 0.0},
                "stream": False,
            },
            timeout=60.0,
        )
        response.raise_for_status()
        raw = response.json()["message"]["content"].strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            result = json.loads(m.group(0))
            logger.info(
                f"[CarrierDetector] Fingerprint: "
                f"unique={result.get('unique_strings', [])}, "
                f"splitter='{result.get('suggested_splitter', '')}'"
            )
            return result
    except Exception as e:
        logger.warning(f"[CarrierDetector] Fingerprint fallito: {e}")
    return {"unique_strings": [], "suggested_splitter": ""}


def enrich_carrier_yaml(
    carrier_name: str,
    markdown: str,
    config_dir: Path,
    ollama_url: str,
    ollama_model: str,
) -> str | None:
    """
    Arricchisce il YAML del carrier con pattern univoci trovati tramite LLM.
    Chiamato dopo la generazione del template (process_invoice_with_sample).
    Restituisce lo splitter suggerito dall'LLM se trovato, altrimenti None.

    Flusso:
    - Chiama _llm_fingerprint_carrier sul markdown completo
    - Aggiunge i unique_strings come patterns nel YAML esistente
    - Combina con i pattern già presenti (deduplicati)
    - Restituisce suggested_splitter per uso in template_generator
    """
    carrier_slug = re.sub(r"[^\w]", "_", carrier_name.lower()).strip("_")
    yaml_path = config_dir / f"{carrier_slug}.yaml"

    fingerprint = _llm_fingerprint_carrier(markdown, ollama_url, ollama_model)
    new_patterns = [
        re.escape(s) for s in fingerprint.get("unique_strings", [])
        if len(s) >= 4
    ]
    suggested_splitter = fingerprint.get("suggested_splitter", "") or None

    if not yaml_path.exists():
        logger.warning(f"[CarrierDetector] YAML non trovato per enrichment: {yaml_path.name}")
        return suggested_splitter

    try:
        config = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        existing_patterns = config.get("patterns", [])
        # Unione deduplicata preservando l'ordine
        all_patterns = list(dict.fromkeys(existing_patterns + new_patterns))
        config["patterns"] = all_patterns

        with open(yaml_path, "w", encoding="utf-8") as f:
            f.write(f"# src/config/carriers/{carrier_slug}.yaml\n")
            f.write("# Aggiornato con fingerprint LLM\n\n")
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

        logger.info(
            f"[CarrierDetector] YAML arricchito: {yaml_path.name} | "
            f"patterns aggiunti: {new_patterns}"
        )
    except Exception as e:
        logger.warning(f"[CarrierDetector] Impossibile aggiornare YAML: {e}")

    return suggested_splitter
def detect_or_create_carrier(
    markdown: str,
    config_dir: Path,
    ollama_url: str,
    ollama_model: str,
) -> str:
    """
    Identifica il corriere dal markdown della fattura.

    STEP 1 — Keyword match (veloce, zero LLM):
        Cerca le keyword del YAML nel testo completo.

    STEP 2 — Pattern match (regex, zero LLM):
        Cerca i pattern regex del YAML. Utile quando il nome è solo nel logo
        (immagine non leggibile da Docling) ma il testo ha codici univoci.

    STEP 3 — LLM identification (1 chiamata, solo se i primi due falliscono):
        Manda l'header al modello per identificazione.

    STEP 4 — YAML auto-creation:
        Se l'LLM trova il corriere, crea il YAML per il futuro.
    """
    if config_dir.exists():
        for yaml_file in config_dir.glob("*.yaml"):
            try:
                config = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
                if not config:
                    continue

                carrier_name = config.get("carrier_name", yaml_file.stem.upper())

                # STEP 1: keyword match
                for keyword in config.get("keywords", []):
                    if str(keyword).lower() in markdown.lower():
                        logger.info(
                            f"[CarrierDetector] '{carrier_name}' trovato via keyword "
                            f"'{keyword}' ({yaml_file.name})"
                        )
                        return carrier_name

                # STEP 2: pattern match (regex)
                for pattern in config.get("patterns", []):
                    try:
                        if re.search(pattern, markdown, re.IGNORECASE):
                            logger.info(
                                f"[CarrierDetector] '{carrier_name}' trovato via pattern "
                                f"'{pattern}' ({yaml_file.name})"
                            )
                            return carrier_name
                    except re.error:
                        logger.warning(f"[CarrierDetector] Pattern regex non valido in {yaml_file.name}: '{pattern}'")

            except Exception as e:
                logger.error(f"[CarrierDetector] Errore lettura {yaml_file.name}: {e}")
                continue

    # STEP 3: LLM
    header = markdown[:1500]
    carrier_name = _llm_identify_carrier(header, ollama_url, ollama_model)

    if carrier_name == "UNKNOWN":
        logger.warning("[CarrierDetector] Corriere non identificato.")
        return "unknown"

    # STEP 4: crea YAML per il futuro
    try:
        _create_yaml(carrier_name, header, config_dir)
    except Exception as e:
        logger.warning(f"[CarrierDetector] Impossibile creare YAML per '{carrier_name}': {e}")

    return carrier_name