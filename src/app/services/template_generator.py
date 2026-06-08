# src/app/services/template_generator.py
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from app.models import CarrierSample, TemplateConfig, TemplateField

logger = logging.getLogger(__name__)


def _find_best_splitter(markdown_text: str) -> tuple[str, int]:
    known_candidates = [
        "Groupage :",
        "Riferimenti:",
        "RIFERIMENTO CORRISPONDENTE:",
        "Rif. Mitt.",
        "Spedizione N.",
        "Lettera di Vettura",
        "Tracking:",
        "Consegna:",
        "Our Reference",
    ]

    best_candidate = "\n\n"
    max_count = 1
    PRIORITY_THRESHOLD = 1.5

    for candidate in known_candidates:
        # FIX: usa regex con \s+ invece di count() esatto
        # Permette di trovare "Groupage            :" anche se nel template è "Groupage :"
        pattern = re.escape(candidate).replace(r"\ ", r"\s+")
        matches = re.findall(pattern, markdown_text)
        count = len(matches)
        required = max_count * PRIORITY_THRESHOLD if best_candidate != "\n\n" else 1
        if count >= required:
            max_count = count
            best_candidate = matches[0] if matches else candidate  # testo letterale dal doc

    if best_candidate == "\n\n":
        line_counts: dict[str, int] = {}
        for line in markdown_text.splitlines():
            stripped = line.strip()
            if 5 < len(stripped) < 60 and not re.fullmatch(r"[\d\s\.,\-]+", stripped):
                line_counts[stripped] = line_counts.get(stripped, 0) + 1
        for line, count in line_counts.items():
            if count > max_count:
                max_count = count
                best_candidate = line

    logger.info(f"[TemplateGen] Splitter: '{best_candidate}' ({max_count} occorrenze)")
    return best_candidate, max_count

def _build_extraction_prompt(
    carrier_name: str,
    fields: list[TemplateField],
    sample_block: str,
) -> str:
    """
    Costruisce il prompt di estrazione da salvare nel template.
    Chiamato una sola volta alla creazione, poi riusato verbatim ad ogni fattura.
    """
    fields_lines = []
    for f in fields:
        line = f'- "{f.name}": {f.description}. Esempio: "{f.example}".'
        if f.hint:
            line += f" Dove trovarlo: {f.hint}."
        fields_lines.append(line)

    return (
        f"Sei un estrattore dati per fatture del corriere {carrier_name}.\n"
        f"Ricevi uno o più blocchi di testo, ciascuno descrive UNA spedizione.\n\n"
        f"CAMPI DA ESTRARRE:\n" + "\n".join(fields_lines) + "\n\n"
        "NOTA SUL FORMATO:\n"
        "- Il testo proviene da PDF, i valori possono essere su righe separate dall'etichetta.\n"
        "- Per 'total_amount': il valore può trovarsi sulla riga successiva a 'TOTALE: EUR', "
        "oppure 'TOTALE: EUR' non ha valore esplicito e il totale è l'ultimo importo sulla riga "
        "dei costi (es. '620,00 620,00' → totale 620,00; '1,00 0,11 1,11' → totale 1,11).\n"
        "- Cerca sempre il valore corretto in entrambe le posizioni.\n\n"
        "- Per 'tracking_number': ignorare codici preceduti da 'Borderò N°:' o 'Borderò:' "
        "sulla riga immediatamente precedente — quelli sono numeri di spedizione collettiva, "
        "non il tracking della singola spedizione. Il tracking corretto si trova subito dopo.\n"
        f"ESEMPIO DI BLOCCO:\n---\n{sample_block[:600]}\n---\n\n"
        "REGOLE:\n"
        "- Restituisci un array JSON: [{...}, {...}]\n"
        "- Un oggetto per ogni spedizione trovata\n"
        '- Usa stringa vuota "" per i campi assenti\n'
        "- Importi con virgola italiana (es. \"253,62\")\n"
        "- Date nel formato trovato nel testo\n"
        "- Solo il JSON, nessuna spiegazione"
    )


def generate_template_from_sample(
    carrier_name: str,
    markdown_sample: str,
    sample: CarrierSample,
    output_path: Path,
    ollama_url: str,   # non usato qui, mantenuto per firma coerente con services
    ollama_model: str, # non usato qui
) -> bool:
    """
    Genera il template JSON dal campione utente (CarrierSample unificato).
    Nessuna chiamata LLM: la qualità viene dagli hint forniti dall'utente.
    Sostituisce generate_template_from_sample + generate_template_from_sample_v2.
    """
    splitter, splitter_count = _find_best_splitter(markdown_sample)
    splitter_pattern = re.escape(splitter).replace(r"\ ", r"\s+")
    blocks = re.split(splitter_pattern, markdown_sample)
    sample_block = (blocks[1] if len(blocks) > 1 else blocks[0])[:1000]  # ← aggiungere


    # Calcola extraction_block_chars dalla dimensione media dei blocchi reali
    real_blocks = blocks[1:6] if len(blocks) > 1 else blocks
    avg_chars = int(sum(len(b.strip()) for b in real_blocks) / max(len(real_blocks), 1))
    extraction_block_chars = min(2000, max(800, int(avg_chars * 1.5)))

    # model_dump() restituisce sempre FieldInput (il validator unificato converte
    # le stringhe semplici in FieldInput automaticamente prima di arrivare qui)
    fields = []
    for field_name, field_input in sample.model_dump().items():
        if field_input is not None:
            fields.append(TemplateField(
                name=field_name,
                description=field_input.get("description") or field_name.replace("_", " "),
                example=field_input.get("value", ""),
                hint=field_input.get("hint", ""),
            ))

    if not fields:
        logger.error("[TemplateGen] Sample vuoto, nessun campo fornito.")
        return False

    extraction_prompt = _build_extraction_prompt(carrier_name, fields, sample_block)

    # Versioning automatico
    carrier_slug = re.sub(r"[^\w]", "_", carrier_name.lower()).strip("_")
    existing = list(output_path.parent.glob(f"{carrier_slug}_v*.json"))
    version = len(existing) + 1

    template = TemplateConfig(
        carrier_name=carrier_name.upper(),
        version=version,
        created_at=datetime.now().isoformat(timespec="seconds"),
        shipment_splitter=splitter,
        batch_size=min(10, max(1, splitter_count // 2)),
        extraction_block_chars=extraction_block_chars,
        fields=fields,
        extraction_prompt=extraction_prompt,
    )
    template_dict = template.model_dump()

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Salva versione numerata (es. eurotir_v2.json)
    versioned_path = output_path.parent / f"{carrier_slug}_v{version}.json"
    with open(versioned_path, "w", encoding="utf-8") as f:
        json.dump(template_dict, f, indent=2, ensure_ascii=False)

    # Salva come "latest" (es. eurotir.json) — usato dal factory per default
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(template_dict, f, indent=2, ensure_ascii=False)

    logger.info(
        f"[TemplateGen] Template salvato: {versioned_path.name} "
        f"({len(fields)} campi, batch_size={template.batch_size}, "
        f"block_chars={extraction_block_chars})"
    )

    # Crea lo YAML per il carrier detector se non esiste già
    yaml_path = output_path.parent.parent / "carriers" / f"{carrier_slug}.yaml"
    if not yaml_path.exists():
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_content = (
            f"carrier_name: {carrier_name.upper()}\n"
            f"keywords:\n"
            f"  - {carrier_name.lower()}\n"
            f"  - {carrier_slug}\n"
        )
        yaml_path.write_text(yaml_content, encoding="utf-8")
        logger.info(f"[TemplateGen] YAML carrier creato: {yaml_path.name}")

    return True