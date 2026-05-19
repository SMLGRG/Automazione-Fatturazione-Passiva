# src/app/parsers/factory.py
import logging
import yaml
from pathlib import Path
from app.parsers import BaseParser, GenericParser

logger = logging.getLogger(__name__)

# Registro globale dei parser specifici.
# Chiave: nome del carrier standardizzato (lowercase).
# Valore: la classe specifica del parser ereditata da BaseParser.
CARRIER_PARSERS: dict[str, type[BaseParser]] = {
    # I parser verticali (es. "dhl": DhlParser) verranno registrati qui in futuro
}


def get_parser(carrier_name: str) -> BaseParser:
    """
    In base al nome del trasportatore, restituisce l'istanza del parser corretto.
    Se non esiste una classe specifica, ripiega sul GenericParser di sicurezza.
    """
    parser_class = CARRIER_PARSERS.get(carrier_name.lower(), GenericParser)
    logger.info(f"Assegnato parser: {parser_class.__name__} per il carrier '{carrier_name}'")
    return parser_class()


def detect_carrier(markdown_content: str, config_dir: Path) -> str:
    """
    Analizza il testo in Markdown della fattura alla ricerca delle parole chiave
    definite nei file di configurazione (.yaml) dei singoli vettori.
    Ritorna il nome del carrier individuato o 'unknown'.
    """
    if not config_dir.exists():
        logger.warning(f"Directory di configurazione non trovata: {config_dir}")
        return "unknown"

    for yaml_file in config_dir.glob("*.yaml"):
        try:
            config = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            if not config or "keywords" not in config:
                continue
                
            carrier_name = config.get("carrier_name", yaml_file.stem.upper())
            
            # Controllo se almeno una delle keyword è presente nel testo (case-insensitive)
            for keyword in config["keywords"]:
                if keyword.lower() in markdown_content.lower():
                    logger.info(f"Carrier identificato automaticamente tramite keyword '{keyword}': {carrier_name}")
                    return carrier_name
                    
        except Exception as e:
            logger.error(f"Errore durante la lettura del file di configurazione {yaml_file.name}: {e}")
            continue

    logger.warning("Impossibile identificare il trasportatore dalle parole chiave presenti nel testo.")
    return "unknown"