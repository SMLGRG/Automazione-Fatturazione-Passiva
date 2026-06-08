import json
import logging
import re
import httpx
from pathlib import Path
from app.parsers import BaseParser
from app.models import InvoiceExtraction, Shipment
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


class ConfigurableParser(BaseParser):
    """
    Parser dinamico basato su un file di configurazione JSON.
    Pipeline: PDF → Docling → MD → split deterministico → [LLM Normalizer] → LLM Extractor → JSON → CSV

    Fix applicati:
      FIX 1 — Tracking cleanup: estrae solo il primo codice valido dalla stringa tracking
      FIX 2 — Recovery pass: ritenta i campi mancanti blocco per blocco dopo il batch
      FIX 3 — Block 0: estrae la prima spedizione dal blocco header+prima_spedizione (solo se necessario)
      FIX 4 — Normalizzazione adattiva: saltata se il testo è già leggibile (es. Galardi)
      FIX 5 — Header extraction saltata se il template non ha campi di testata
      FIX 6 — extraction_block_chars dinamico da template JSON (carrier-agnostico)
    """

    def __init__(
        self,
        config_json_path: Path,
        ollama_url: str = "http://localhost:11434",
        ollama_model: str = "qwen2.5:7b",
    ):
        super().__init__()
        self.config_path = config_json_path
        self.ollama_url = ollama_url
        self.ollama_model = ollama_model
        with open(config_json_path, "r", encoding="utf-8") as f:
            self.rules = json.load(f)

    def post_process(self, markdown: str) -> str:
        return markdown

    def has_regex_rules(self) -> bool:
        return True

    # ------------------------------------------------------------------ #
    #  UTILITIES                                                           #
    # ------------------------------------------------------------------ #

    def _search_group(self, pattern: str, text: str) -> str:
        if not pattern:
            return ""
        try:
            match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
            if match and match.groups():
                return match.group(1).strip()
        except Exception as e:
            logger.debug(f"Errore regex '{pattern}': {e}")
        return ""

    def _clean_float(self, val_str: str) -> float | None:
        if not val_str:
            return None
        cleaned = re.sub(r"[^\d.,-]", "", val_str).strip()
        if not cleaned:
            return None
        try:
            if "," in cleaned and "." in cleaned:
                if cleaned.find(".") < cleaned.find(","):
                    cleaned = cleaned.replace(".", "").replace(",", ".")
                else:
                    cleaned = cleaned.replace(",", "")
            elif "," in cleaned and "." not in cleaned:
                cleaned = cleaned.replace(",", ".")
            return float(cleaned)
        except Exception:
            logger.warning(f"Impossibile convertire '{val_str}' in float.")
            return None

    def _clean_company_name(self, name: str | None) -> str | None:
        """Rimuove la parte indirizzo dal nome azienda.
        'BEKINTEX - 9230 - Wetteren - VO - BE' → 'BEKINTEX'
        """
        if not name:
            return name
        cleaned = re.sub(r'\s*-\s*\d{3,6}\b.*$', '', name).strip()
        return cleaned if cleaned else name
    
    def _needs_normalization(self, block: str) -> bool:
        """
        FIX 4 — Decide se i blocchi necessitano di normalizzazione LLM.
        EUROTIR: un valore per riga, molte righe vuote → ratio > 0.40 → normalizza.
        Galardi: testo denso, tabelle markdown → ratio basso → salta (risparmia 6-8 min).
        Carrier-agnostico: si basa solo sulla struttura del testo, non sul nome del carrier.
        """
        if len(block.strip()) < 50:
            return False
        lines = block.splitlines()
        non_empty = [la for la in lines if la.strip()]
        if not non_empty:
            return False
        avg_len = sum(len(la.strip()) for la in non_empty) / len(non_empty)
        # EUROTIR: un valore per riga → avg ~12-20 chars → normalizza
        # Galardi: testo tabulare → avg ~60-80 chars → non normalizza
        return avg_len < 30

    def _call_ollama(self, prompt: str, timeout: float = 60.0) -> str:
        """Helper: esegue una chiamata Ollama e restituisce il testo grezzo della risposta."""
        response = httpx.post(
            f"{self.ollama_url.rstrip('/')}/api/chat",
            json={
                "model": self.ollama_model,
                "messages": [{"role": "user", "content": prompt}],
                "options": {"temperature": 0.0},
                "stream": False,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()["message"]["content"].strip()

    # ------------------------------------------------------------------ #
    #  LLM METHODS                                                         #
    # ------------------------------------------------------------------ #

    def _extract_block_llm(self, block: str) -> dict:
        """Legacy: estrae da un singolo blocco con prompt fisso (template vecchio senza extraction_prompt)."""
        prompt = (
            "Estrai i dati da questo testo di spedizione e restituisci SOLO un oggetto JSON valido.\n"
            "Campi richiesti (stringa vuota se assente, NON null):\n"
            '  "tracking_number": codice spedizione es. "01/23/500072"\n'
            '  "shipment_date": data gg/mm/aaaa es. "05/01/2023"\n'
            '  "sender_name": nome mittente es. "MARCHI & FILDI SPA"\n'
            '  "recipient_name": nome destinatario es. "BEKINTEX"\n'
            '  "destination_city": città destinazione es. "Wetteren"\n'
            '  "destination_postal_code": CAP es. "9230"\n'
            '  "destination_country": codice paese 2 lettere es. "BE"\n'
            '  "weight_kg": peso con virgola es. "1.547,20", vuoto se assente\n'
            '  "service_type": tipo resa es. "DAP"\n'
            '  "total_amount": importo totale con virgola es. "620,00"\n\n'
            f"TESTO SPEDIZIONE:\n{block[:800]}\n\n"
            "Rispondi SOLO con il JSON, nessun testo aggiuntivo."
        )
        try:
            raw = self._call_ollama(prompt, timeout=60.0)
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                return json.loads(m.group(0))
            logger.warning(f"[Parser LLM] Nessun JSON: {raw[:200]}")
        except json.JSONDecodeError as e:
            logger.warning(f"[Parser LLM] JSON non parsabile: {e}")
        except Exception as e:
            logger.warning(f"[Parser LLM] Chiamata fallita: {e}")
        return {}

    def _normalize_blocks_llm(self, blocks: list[str], timeout: float = 120.0) -> list[str]:

        """
        Fase 1 (opzionale) — Normalizza N blocchi di markdown sparso in testo leggibile.
        Chiamata solo se _needs_normalization() restituisce True.
        Fallback sui blocchi originali in caso di errore o timeout.
        """
        if not blocks:
            return blocks

        numbered = "\n\n".join(
            f"---BLOCCO {i+1}---\n{b.strip()[:2000]}"
            for i, b in enumerate(blocks)
        )
        prompt = (
            "Normalizza questi blocchi di testo estratti da un PDF con formato sparso "
            "(un valore per riga, molte righe vuote tra etichette e valori).\n"
            "Per ogni blocco: unisci etichette e valori correlati sulla stessa riga, "
            "rimuovi righe vuote eccessive, preserva ESATTAMENTE tutti i numeri, "
            "codici, date e importi senza modificarli.\n"
            "NON interpretare, NON aggiungere informazioni, NON rimuovere dati.\n\n"
            f"BLOCCHI:\n{numbered}\n\n"
            f"Restituisci SOLO un array JSON con esattamente {len(blocks)} stringhe "
            "normalizzate nello stesso ordine dei blocchi. Solo il JSON."
        )
        try:
            raw = self._call_ollama(prompt, timeout=timeout)
            arr_match = re.search(r"\[.*\]", raw, re.DOTALL)
            if arr_match:
                normalized = json.loads(arr_match.group(0))
                if isinstance(normalized, list) and len(normalized) == len(blocks):
                    logger.info(f"[Parser Normalize] {len(blocks)} blocchi normalizzati")
                    return [str(n) for n in normalized]
                logger.warning(
                    f"[Parser Normalize] Mismatch: attesi {len(blocks)}, "
                    f"ricevuti {len(normalized) if isinstance(normalized, list) else 'non-lista'}"
                )
        except Exception as e:
            logger.warning(f"[Parser Normalize] Fallito, uso blocchi originali: {e}")
        return blocks

    def _extract_batch_llm(self, blocks: list[str], extraction_prompt: str) -> list[dict]:
        """
        Fase 2 — Estrae N blocchi in una sola chiamata Ollama.
        FIX 6: usa extraction_block_chars dal template (carrier-agnostico).
        Restituisce sempre una lista della stessa lunghezza dell'input.
        """
        # Legge il limite dal template; default 1000 se non presente (retrocompatibile)
        block_chars = self.rules.get("extraction_block_chars", 1000)

        numbered = "\n\n".join(
            f"---SPEDIZIONE {i+1}---\n{b.strip()[:block_chars]}"
            for i, b in enumerate(blocks)
        )
        prompt = (
            extraction_prompt + "\n\n"
            "TESTO:\n" + numbered + "\n\n"
            f"Restituisci un array JSON con esattamente {len(blocks)} oggetti, "
            "uno per ogni ---SPEDIZIONE---. Solo il JSON."
        )
        try:
            raw = self._call_ollama(prompt, timeout=120.0)
            arr_match = re.search(r"\[.*\]", raw, re.DOTALL)
            if arr_match:
                results = json.loads(arr_match.group(0))
                if isinstance(results, list):
                    while len(results) < len(blocks):
                        results.append({})
                    return results[:len(blocks)]
            logger.warning(f"[Parser Batch] Nessun array JSON: {raw[:200]}")
        except json.JSONDecodeError as e:
            logger.warning(f"[Parser Batch] JSON non parsabile: {e}")
        except Exception as e:
            logger.warning(f"[Parser Batch] Chiamata fallita: {e}")
        return [{} for _ in blocks]

    def _extract_first_shipment_llm(self, block0: str, extraction_prompt: str) -> dict:
        # Trova il primo tracking NON preceduto da "Borderò" sulla riga prima
        relevant_text = block0[:2000]
        for m in re.finditer(r'\d{2}/\d{2}/\d{6}', block0):
            line_start = block0.rfind('\n', 0, m.start())
            line_before = block0[line_start:m.start()].strip()
            if re.search(r'border[oò°]', line_before, re.IGNORECASE):
                continue  # salta il Borderò
            relevant_text = block0[max(0, m.start() - 30):m.start() + 2000]
            break

        prompt = (
            extraction_prompt + "\n\n"
            "IMPORTANTE: estrai SOLO i dati della singola spedizione nel testo seguente. "
            "Ignora intestazione fattura, IVA, totali fattura, dati aziendali.\n\n"
            f"TESTO:\n{relevant_text}\n\n"
            "Restituisci un array JSON con UN solo oggetto. Solo il JSON."
        )
        try:
            raw = self._call_ollama(prompt, timeout=60.0)
            arr_match = re.search(r"\[.*\]", raw, re.DOTALL)
            if arr_match:
                results = json.loads(arr_match.group(0))
                if isinstance(results, list) and results:
                    return results[0]
            obj_match = re.search(r"\{.*\}", raw, re.DOTALL)
            if obj_match:
                return json.loads(obj_match.group(0))
        except Exception as e:
            logger.warning(f"[Parser Block0] Errore: {e}")
        return {}

    def _extract_header_llm(self, header_text: str, fields: list | None = None) -> dict:
        """Estrae i campi di testata usando gli hint del template se disponibili."""
        HEADER_NAMES = {"invoice_number", "invoice_date", "total_invoice_amount"}
        header_fields = [f for f in (fields or []) if f.get("name") in HEADER_NAMES]

        if header_fields:
            field_lines = "\n".join(
                f'- "{f["name"]}": {f["description"]}. '
                f'Esempio: "{f["example"]}". '
                + (f'Dove trovarlo: {f["hint"]}.' if f.get("hint") else "")
                for f in header_fields
            )
            fields_json = "{" + ", ".join(f'"{f["name"]}": ""' for f in header_fields) + "}"
        else:
            field_lines = '- "invoice_number", "invoice_date", "total_invoice_amount"'
            fields_json = '{"invoice_number": "", "invoice_date": "", "total_invoice_amount": ""}'

        prompt = (
            "Dal testo seguente estrai i dati di intestazione fattura.\n"
            "CAMPI DA ESTRARRE:\n" + field_lines + "\n\n"
            "Restituisci SOLO un oggetto JSON:\n"
            f"{fields_json}\n\n"
            f"TESTO:\n{header_text}\n\nRispondi SOLO con il JSON."
        )
        try:
            raw = self._call_ollama(prompt, timeout=60.0)
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                return json.loads(m.group(0))
        except Exception as e:
            logger.warning(f"[Parser Header] Errore: {e}")
        return {}

    # ------------------------------------------------------------------ #
    #  MAIN PIPELINE                                                       #
    # ------------------------------------------------------------------ #

    def parse_local(self, markdown: str, debug_dir: Path | None = None) -> InvoiceExtraction:
        logger.info(f"[Parser] Avvio estrazione con template: {self.config_path.name}")

        splitter = self.rules.get("shipment_splitter", "").strip()
        has_new_format = bool(self.rules.get("extraction_prompt"))
        headers = self.rules.get("header_selectors", {})

        # --- SPLIT IN BLOCCHI ---
        blocks = [markdown]
        if splitter:
            splitter_pattern = re.escape(splitter).replace(r"\ ", r"\s+")
            if re.search(splitter_pattern, markdown):
                blocks = re.split(splitter_pattern, markdown)
                logger.info(f"[Parser] Splitter '{splitter}' → {len(blocks)} blocchi")

        # Block0 grande in new_format viene escluso dal batch e gestito da FIX3
        # Evita che dati dell'header (peso IVA, tabelle) contaminino il primo blocco spedizione
        if has_new_format and len(blocks) > 1 and len(blocks[0].strip()) > 500:
            shipment_blocks = [b for b in blocks[1:] if len(b.strip()) >= 20]
            logger.info("[Parser] Block0 grande → escluso dal batch, gestito da FIX3")
        else:
            shipment_blocks = [b for b in blocks if len(b.strip()) >= 20]

        # --- ESTRAZIONE HEADER ---
        # FIX 5: salta la chiamata LLM se il template non ha campi di testata
        HEADER_NAMES = {"invoice_number", "invoice_date", "total_invoice_amount"}
        if has_new_format:
            has_header_fields = any(
                f.get("name") in HEADER_NAMES
                for f in self.rules.get("fields", [])
            )
            if has_header_fields:
                header_data = self._extract_header_llm(blocks[0][:1500], self.rules.get("fields", []))
                invoice_num = header_data.get("invoice_number") or "NON_RILEVATO"
                invoice_date = header_data.get("invoice_date") or "NON_RILEVATO"
                total_amount_float = self._clean_float(str(header_data.get("total_invoice_amount") or ""))
            else:
                logger.info("[Parser] Nessun campo testata nel template, estrazione header saltata")
                invoice_num = "NON_RILEVATO"
                invoice_date = "NON_RILEVATO"
                total_amount_float = None
        else:
            invoice_num = self._search_group(headers.get("invoice_number", ""), markdown) or "NON_RILEVATO"
            invoice_date = self._search_group(headers.get("invoice_date", ""), markdown) or "NON_RILEVATO"
            total_amount_float = self._clean_float(
                self._search_group(headers.get("total_invoice_amount", ""), markdown)
            )

        # --- CONVERSIONE DICT → SHIPMENT ---
        def _to_shipment(data: dict) -> Shipment | None:
            # Gate: almeno uno dei campi core presenti
            has_content = any([
                data.get("recipient_name") or data.get("destinatario"),
                data.get("sender_name") or data.get("mittente"),
                data.get("total_amount") or data.get("prezzo"),
                data.get("tracking_number") or data.get("numero_doc"),
            ])
            if not has_content:
                return None

            # FIX 1: estrai solo il primo codice tracking valido
            raw_tracking = str(data.get("tracking_number") or data.get("numero_doc") or "").strip()
            if re.match(r"^\d{2}/\d{4}/\d{6}$", raw_tracking):
                tracking = ""  # formato Borderò (DD/YYYY/NNNNNN), non è un tracking
            else:
                m = re.search(r"\d{2}/\d{2}/\d{6}", raw_tracking)
                tracking = m.group(0) if m else raw_tracking

            # Campi core con mapping da alias italiani
            ALIAS_KEYS = {
                "tracking_number", "numero_doc",
                "shipment_date", "data_doc",
                "sender_name", "mittente",
                "recipient_name", "destinatario",
                "destination_city", "destinazione",
                "destination_postal_code", "cap",
                "weight_kg", "peso",
                "total_amount", "prezzo",
            }

            # Tutto ciò che non è un alias core passa come campo extra (es. destination_country,
            # service_type, orario_consegna, container_id — qualunque cosa l'utente abbia richiesto)
            extra_fields = {
                k: v for k, v in data.items()
                if k not in ALIAS_KEYS and v not in ("", None)
            }
            dest_city = data.get("destination_city") or data.get("destinazione")
            dest_cap = data.get("destination_postal_code") or data.get("cap")
            raw_recipient = data.get("recipient_name") or data.get("destinatario") or ""

            # Fallback deterministico: se city o cap mancano ma sono nell'indirizzo completo
            # es. "SITPM DIVISION TISSAGE - 38500 - Saint-Nicolas-de-Macherin - 38 - FR"
            if (not dest_city or not dest_cap) and raw_recipient:
                addr_m = re.search(r'-\s*(\d{4,6})\s*-\s*([^-\n]+?)(?:\s*-|$)', raw_recipient)
                if addr_m:
                    if not dest_cap:
                        dest_cap = addr_m.group(1).strip()
                    if not dest_city:
                        dest_city = addr_m.group(2).strip()

            # Fix 3: rimuove codice paese finale dalla città — applicato SEMPRE
            # "VALONGO DA VOUGA PT" → "VALONGO DA VOUGA"
            if dest_city:
                dest_city = re.sub(r'\s+[A-Z]{2}$', '', dest_city).strip() or dest_city

            # Fix 4: normalizza CAP con spazio → trattino — applicato SEMPRE
            # "3754 905" → "3754-905", "6201 951" → "6201-951"
            if dest_cap:
                dest_cap = re.sub(r'^(\d{4})\s+(\d{3})$', r'\1-\2', dest_cap.strip()) or dest_cap

            return Shipment(
                tracking_number=tracking or None,
                shipment_date=data.get("shipment_date") or data.get("data_doc") or None,
                sender_name=self._clean_company_name(data.get("sender_name") or data.get("mittente") or None),
                recipient_name=self._clean_company_name(data.get("recipient_name") or data.get("destinatario") or None),
                destination_city=dest_city or None,
                destination_postal_code=dest_cap or None,
                weight_kg=self._clean_float(str(data.get("weight_kg") or data.get("peso") or "")),
                total_amount=self._clean_float(str(data.get("total_amount") or data.get("prezzo") or "")),
                **extra_fields,
            )

        # ---------------------------------------------------------------- #
        #  MODALITÀ NUOVO FORMATO (extraction_prompt presente)              #
        # ---------------------------------------------------------------- #
        shipments_extracted = []

        if has_new_format and shipment_blocks:
            extraction_prompt = self.rules["extraction_prompt"]
            batch_size = max(1, self.rules.get("batch_size", 3))
            block_chars = self.rules.get("extraction_block_chars", 1000)
            effective_batch_size = max(1, min(batch_size, int(8000 / max(block_chars, 100))))
            if effective_batch_size < batch_size:
                logger.info(
                    f"[Parser] batch_size ridotto: {batch_size} → {effective_batch_size} "
                    f"(blocchi da {block_chars} chars, limite 8000 chars/chiamata)"
                )
            avg_block_chars = sum(len(b.strip()) for b in shipment_blocks) / max(len(shipment_blocks), 1)
            # Blocchi grandi → batch più piccoli per non eccedere il context LLM
            normalize_batch_size = max(1, min(4, int(4000 / max(avg_block_chars, 100))))
            # Timeout scala con dimensione blocchi × batch size
            normalize_timeout = max(120.0, min(300.0, avg_block_chars * 0.08 * normalize_batch_size + 60))
            # Su CPU con blocchi grandi, Ollama è seriale: workers=1 evita timeout a cascata
            max_normalize_workers = 1 if avg_block_chars > 800 else 4
            logger.info(
                f"[Parser] Normalizzazione: batch_size={normalize_batch_size}, "
                f"timeout={normalize_timeout:.0f}s, workers={max_normalize_workers}"
            )

            # FASE 1: NORMALIZZAZIONE ADATTIVA (FIX 4)
            # Controlla il primo blocco significativo come campione rappresentativo
            sample_block = next((b for b in shipment_blocks if len(b.strip()) > 100), "")
            if self._needs_normalization(sample_block):
                logger.info("[Parser] Testo sparso rilevato → normalizzazione attiva")
                
                # Cache: se esiste già, salta le chiamate LLM
                cache_path = debug_dir / "normalized_cache.json" if debug_dir else None
                if cache_path and cache_path.exists():
                    try:
                        cached = json.loads(cache_path.read_text(encoding="utf-8"))
                        if cached.get("block_count") == len(shipment_blocks):
                            logger.info("[Parser] Normalizzazione da cache, LLM saltato")
                            normalized_blocks = cached["blocks"]
                        else:
                            raise ValueError("block_count mismatch")
                    except Exception:
                        cache_path = None  # cache non valida, ricalcola

                if not (cache_path and cache_path.exists()):
                    chunks = [
                        shipment_blocks[i:i + normalize_batch_size]
                        for i in range(0, len(shipment_blocks), normalize_batch_size)
                    ]
                    results: list[list[str]] = [[] for _ in chunks]
                    with ThreadPoolExecutor(max_workers=max_normalize_workers) as executor:
                        futures = {
                            executor.submit(self._normalize_blocks_llm, chunk, normalize_timeout): idx
                            for idx, chunk in enumerate(chunks)
                        }
                        for future in futures:
                            idx = futures[future]
                            results[idx] = future.result()
                    normalized_blocks = [b for sublist in results for b in sublist]

                    if cache_path:
                        cache_path.write_text(
                            json.dumps({"block_count": len(shipment_blocks), "blocks": normalized_blocks},
                                    ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
            else:
                logger.info("[Parser] Testo già leggibile → normalizzazione saltata")
                normalized_blocks = shipment_blocks

            if debug_dir:
                (debug_dir / "debug_normalize.json").write_text(
                    json.dumps({
                        "template": self.config_path.name,
                        "normalization_applied": self._needs_normalization(sample_block),
                        "raw_blocks": [b.strip()[:2000] for b in shipment_blocks],
                        "normalized_blocks": [b[:600] for b in normalized_blocks],
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            # FASE 2: ESTRAZIONE BATCH
            logger.info(f"[Parser] Batch mode: {len(normalized_blocks)} blocchi, batch_size={effective_batch_size}")
            all_extraction_results = []
            block_result_pairs: list[tuple[str, dict]] = []

            for i in range(0, len(normalized_blocks), effective_batch_size):
                batch_norm = normalized_blocks[i:i + effective_batch_size]
                batch_raw = shipment_blocks[i:i + effective_batch_size]
                results = self._extract_batch_llm(batch_norm, extraction_prompt)
                logger.info(
                    f"[Parser] Batch {i // effective_batch_size + 1}: {len(results)} risultati | "
                    f"preview: {json.dumps(results, ensure_ascii=False)[:300]}"
                )
                all_extraction_results.extend(results)
                for raw_block, data in zip(batch_raw, results):
                    block_result_pairs.append((raw_block, data))
                    s = _to_shipment(data)
                    if s:
                        shipments_extracted.append(s)

            if debug_dir and all_extraction_results:
                (debug_dir / "debug_extraction.json").write_text(
                    json.dumps(all_extraction_results, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            # FIX 2: RECOVERY PASS
            before_count = len(shipments_extracted)
            shipments_extracted = [s for s in shipments_extracted if s.tracking_number]
            removed = before_count - len(shipments_extracted)
            if removed:
                logger.info(f"[Parser Recovery] Rimossi {removed} blocchi parziali senza tracking dal batch")

            existing_trackings = {s.tracking_number for s in shipments_extracted}
            recovered_count = 0

            for raw_block, orig_data in block_result_pairs:
                has_some_content = any([
                    orig_data.get("recipient_name"),
                    orig_data.get("sender_name"),
                    orig_data.get("shipment_date"),
                ])
                if not has_some_content:
                    continue

                missing: list[str] = []
                if not orig_data.get("total_amount") and not orig_data.get("prezzo"):
                    missing.append("total_amount")
                if not orig_data.get("tracking_number") and not orig_data.get("numero_doc"):
                    missing.append("tracking_number")

                if not missing:
                    continue

                logger.info(f"[Parser Recovery] Recupero {missing} da blocco parziale")
                recovered_results = self._extract_batch_llm([raw_block[:1500]], extraction_prompt)
                recovered = recovered_results[0] if recovered_results else {}
                if not recovered:
                    logger.warning("[Parser Recovery] Nessun dato recuperato, blocco scartato")
                    continue

                merged = {**orig_data, **{k: v for k, v in recovered.items() if v}}
                s = _to_shipment(merged)
                if not s:
                    logger.warning("[Parser Recovery] Shipment non valido dopo merge, blocco scartato")
                    continue

                if s.tracking_number and s.tracking_number in existing_trackings:
                    shipments_extracted = [
                        ex.model_copy(update={"total_amount": s.total_amount})
                        if ex.tracking_number == s.tracking_number and s.total_amount is not None
                        else ex
                        for ex in shipments_extracted
                    ]
                    logger.info(f"[Parser Recovery] Aggiornato total_amount per tracking {s.tracking_number}")
                    recovered_count += 1
                elif s.tracking_number:
                    logger.info(f"[Parser Recovery] Aggiunta spedizione recuperata: {s.tracking_number}")
                    shipments_extracted.append(s)
                    existing_trackings.add(s.tracking_number)
                    recovered_count += 1
                else:
                    logger.warning(
                        f"[Parser Recovery] Tracking non trovato dopo recovery "
                        f"(recipient={s.recipient_name}), blocco scartato"
                    )

            if recovered_count:
                logger.info(f"[Parser Recovery] {recovered_count} spedizioni aggiornate/recuperate")

            # FIX 3: PRIMA SPEDIZIONE DAL BLOCCO 0
            # Attivato solo se block0 è lungo E non è già in shipment_blocks
            # (caso EUROTIR: block0 contiene header+prima spedizione mescolati)
            # Non si attiva per Galardi dove block0 è già processato normalmente
            if (shipments_extracted and len(blocks) > 1
                    and len(blocks[0].strip()) > 500):
                first_data = self._extract_first_shipment_llm(blocks[0], extraction_prompt)
                first_s = _to_shipment(first_data)
                if first_s and first_s.tracking_number and first_s.tracking_number not in existing_trackings:
                    logger.info(f"[Parser Block0] Prima spedizione recuperata: {first_s.tracking_number}")
                    shipments_extracted.insert(0, first_s)
                elif first_s and not first_s.tracking_number:
                    logger.warning("[Parser Block0] Prima spedizione senza tracking, scartata")

        # ---------------------------------------------------------------- #
        #  MODALITÀ LEGACY (template vecchio senza extraction_prompt)       #
        # ---------------------------------------------------------------- #
        else:
            for block in (shipment_blocks or blocks):
                data = self._extract_block_llm(block)
                s = _to_shipment(data)
                if s:
                    shipments_extracted.append(s)

        # --- FALLBACK se nessuna spedizione trovata ---
        if not shipments_extracted:
            logger.warning("[Parser] Nessuna spedizione estratta. Fallback su markdown intero.")
            if has_new_format:
                results = self._extract_batch_llm([markdown[:2000]], self.rules["extraction_prompt"])
            else:
                results = [self._extract_block_llm(markdown[:2000])]
            for data in results:
                s = _to_shipment(data)
                if s:
                    shipments_extracted.append(s)

        return InvoiceExtraction(
            invoice_number=invoice_num,
            invoice_date=invoice_date,
            carrier_name=self.rules.get("carrier_name", "UNKNOWN"),
            total_invoice_amount=total_amount_float,
            shipments=shipments_extracted,
        )