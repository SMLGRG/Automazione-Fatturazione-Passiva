# Fatturazione Passiva — Automazione Fatture Trasportatori

Pipeline 100% locale per leggere le fatture PDF dei trasportatori, estrarne i dati con un'AI e confrontarli con il database aziendale.

---

## Il problema che risolve

Le fatture dei trasportatori (DHL, BRT, FedEx, Galardi…) arrivano in PDF, ognuna con un layout diverso, spesso come scansioni. Estrarle a mano è lento e soggetto a errori. Mandarle a un'AI cloud solleva problemi di privacy e riservatezza dei dati aziendali.

Questo progetto automatizza l'intero processo in locale: dal PDF grezzo al CSV riconciliato, senza che i dati escano mai dalla rete interna.

---

## Come funziona

```
PDF fattura trasportatore
        ↓
  Docling (IBM)
  Converte il PDF in Markdown — OCR incluso per i PDF scansionati
        ↓
  Smart Compressor
  Elimina header, footer e testo ripetuto tra le pagine (~79% di riduzione)
        ↓
  LLM locale via Ollama
  Markdown → JSON strutturato con i dati delle spedizioni
        ↓
  Validazione Pydantic
  Controlla campi obbligatori, range numerici, formati date
        ↓
  Export CSV
  Formato con separatore ; e decimali , — apre direttamente in Excel
        ↓
  Riconciliazione con DB aziendale
  Confronta ogni spedizione con i costi attesi e segnala le discrepanze
```

Il passaggio più interessante è lo **Smart Compressor**: analizza quante volte ogni riga di testo si ripete tra le pagine del documento e rimuove automaticamente tutto il boilerplate (intestazioni aziendali, piè di pagina, note legali ripetute) prima di mandare il testo all'AI. Risultato: meno token, elaborazione più veloce e risultati migliori.

Il sistema riconosce automaticamente il trasportatore mittente leggendo il testo della fattura e applica il parser più adatto. Ogni trasportatore è configurabile con un file YAML, senza toccare il codice.

---

## Avvio

**Prerequisiti: Python 3.11/3.12 e [Ollama](https://ollama.com/download) installati.**

Poi basta fare doppio clic su:
- **`avvia.bat`** su Windows
- **`avvia.sh`** su Mac/Linux

Lo script al primo avvio crea l'ambiente virtuale, installa le dipendenze e scarica il modello AI (~2.5 GB). Dai avvii successivi parte offline in pochi secondi.

L'app è raggiungibile su **`http://localhost:8000`** — si può caricare un PDF, monitorare l'elaborazione e scaricare il CSV direttamente dal browser.

Ollama rileva automaticamente la GPU se disponibile (NVIDIA o Apple Silicon): i tempi scendono da ~30 secondi su CPU a pochi secondi.

---

## Stack

| Componente | Tecnologia |
|---|---|
| Backend / API | FastAPI + Uvicorn |
| PDF → Markdown | Docling (IBM) — OCR + analisi tabelle inclusi |
| AI locale | Ollama (modello configurabile via `.env`) |
| Validazione dati | Pydantic v2 |
| Export e riconciliazione | Pandas |
| Database | SQLite (migrabile a PostgreSQL senza modifiche al codice) |
| Frontend | Jinja2 + HTMX |

---

## Struttura del repository

```
├── src/
│   ├── app/
│   │   ├── parsers/        # Docling + riconoscimento trasportatore (factory pattern)
│   │   ├── extractors/     # Client Ollama + chunking per fatture lunghe
│   │   ├── models/         # Schema Pydantic (fattura, spedizione, stato)
│   │   ├── services/       # Smart compressor, CSV export, riconciliazione
│   │   ├── routers/        # Endpoint FastAPI (invoices, reports, health)
│   │   └── templates/      # UI browser (Jinja2)
│   └── config/
│       └── carriers/       # Configurazione YAML per trasportatore
│
├── tests/
│   ├── unit/               # Test su singoli componenti
│   ├── integration/        # Pipeline completa su PDF campione
│   └── fixtures/           # PDF reali annotati per i test
│
├── avvia.bat               # Avvio Windows
└── avvia.sh                # Avvio Mac/Linux
```

---

## Stato del progetto, cosa mandca

- [x] Pipeline completa: PDF → CSV
- [x] Smart Compressor (riduzione ~79% del testo)
- [x] Riconoscimento automatico trasportatore
- [] Riconciliazione con DB aziendale
- [x] Interfaccia browser minimale (upload, lista fatture, download CSV)
- [ ] Endpoint `/reports` (in sviluppo)
- [ ] UI avanzata (prevista in fase successiva)