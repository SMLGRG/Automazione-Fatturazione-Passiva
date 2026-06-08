import logging
import re
from pathlib import Path
from app.parsers import GenericParser
from app.parsers.configurable_parser import ConfigurableParser

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "config" / "templates"


def get_parser(
    carrier_name: str,
    ollama_base_url: str = "http://localhost:11434",
    ollama_model: str = "qwen2.5:7b",
) -> GenericParser | ConfigurableParser:
    carrier_lower = carrier_name.lower()

    if carrier_lower == "unknown":
        return GenericParser()

    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    carrier_slug = re.sub(r"[^\w]", "_", carrier_lower)
    template_path = TEMPLATES_DIR / f"{carrier_slug}.json"

    if not template_path.exists():
        logger.warning(f"[Factory] Template per '{carrier_name}' non trovato. Usato GenericParser.")
        return GenericParser()

    logger.info(f"[Factory] Assegnato ConfigurableParser basato sul file: {template_path.name}")
    try:
        return ConfigurableParser(template_path, ollama_base_url, ollama_model)
    except Exception as e:
        logger.error(f"[Factory] Template {template_path.name} corrotto: {e}. Ripiego su GenericParser.")
        template_path.unlink(missing_ok=True)
        return GenericParser()