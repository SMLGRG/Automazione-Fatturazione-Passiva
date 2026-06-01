# src/app/parsers/factory.py
import logging
import yaml
from pathlib import Path
from app.parsers import BaseParser, GenericParser

logger = logging.getLogger(__name__)

# Registro parser per carrier specifici.
# Attualmente vuoto: tutti i carrier usano GenericParser + SmartCompressor.
# Per aggiungere un parser custom in futuro: {"nome_carrier": MyParser}
CARRIER_PARSERS: dict[str, type[BaseParser]] = {}


def get_parser(carrier_name: str) -> BaseParser:
    parser_class = CARRIER_PARSERS.get(carrier_name.lower(), GenericParser)
    logger.info(f"Assegnato parser: {parser_class.__name__} per il carrier '{carrier_name}'")
    return parser_class()


def detect_carrier(markdown_content: str, config_dir: Path) -> str:
    if not config_dir.exists():
        logger.warning(f"Directory di configurazione non trovata: {config_dir}")
        return "unknown"

    for yaml_file in config_dir.glob("*.yaml"):
        try:
            config = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            if not config or "keywords" not in config:
                continue

            carrier_name = config.get("carrier_name", yaml_file.stem.upper())

            for keyword in config["keywords"]:
                if keyword.lower() in markdown_content.lower():
                    logger.info(f"Carrier identificato tramite keyword '{keyword}': {carrier_name}")
                    return carrier_name

        except Exception as e:
            logger.error(f"Errore lettura config {yaml_file.name}: {e}")
            continue

    logger.warning("Impossibile identificare il trasportatore dalle parole chiave.")
    return "unknown"