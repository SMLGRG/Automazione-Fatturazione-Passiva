# Automazione Fatturazione Passiva — Web App

Pipeline Python completamente locale per l'estrazione automatica dei dati dalle fatture dei trasportatori e la loro riconciliazione con il database aziendale.

---

## Obiettivo

Le fatture dei trasportatori arrivano in formato PDF, con layout diversi per ogni corriere e spesso come scansioni. Confrontarle manualmente con le spedizioni registrate a sistema è un lavoro ripetitivo, lento e soggetto a errori.

Questo strumento automatizza l'intero flusso: dal PDF grezzo al CSV riconciliato, rilevando automaticamente le discrepanze tra quanto fatturato e quanto presente nel database aziendale.

**Tutto gira in locale e in modo isolato.** I dati aziendali non escono mai dalla rete interna. Non sono richieste API key, account esterni o connessione a internet. Il sistema è progettato per essere avviato con un singolo comando e funzionare immediatamente, senza richiedere l'installazione manuale di dipendenze, runtime o modelli AI — ci pensa Docker.

---

## Flusso di funzionamento

```
PDF Fattura Trasportatore
        ↓
   Docling (IBM)
   Conversione PDF → Markdown strutturato
   [riconoscimento tabelle + OCR per PDF scansionati]
        ↓
   Qwen2.5:3b via Ollama
   Markdown → JSON strutturato
   [output JSON garantito valido tramite Outlines]
        ↓
   Validazione con Pydantic
   [controllo range, campi obbligatori, normalizzazione]
        ↓
   CSV Standardizzato
        ↓
   Confronto con DB Aziendale
        ↓
   Report Discrepanze
```

---

## Avvio e distribuzione

Il progetto è distribuito come repository Git. L'unico prerequisito per l'utente finale è avere **Docker** installato sulla propria macchina. Tutto il resto — Python, le librerie, Ollama, il modello AI — è gestito internamente dai container.

```
git clone <repo>
docker compose up
```

Al primo avvio, Docker scarica le immagini necessarie e il modello LLM (`qwen2.5:3b`, ~2GB) all'interno del container Ollama. Questa operazione è automatica e avviene una sola volta. Dall'avvio successivo tutto parte offline, senza alcun download aggiuntivo. L'applicazione è raggiungibile via browser su `http://localhost:8000`.

### Rilevamento automatico GPU

Il container Ollama è configurato per rilevare automaticamente la presenza di una GPU NVIDIA e utilizzarla se disponibile. In questo caso, l'elaborazione di una fattura scende da ~20–30 secondi (CPU) a pochi secondi. Su macchine senza GPU, il sistema utilizza `qwen2.5:3b` in modalità CPU-only: il modello è stato scelto proprio per il suo ottimo rapporto tra accuratezza e velocità su CPU, senza bisogno di modificare alcuna configurazione.

---

## Stack tecnologico

### Backend

| Tecnologia | Versione | Ruolo | Perché questa scelta |
|---|---|---|---|
| Python | ≥ 3.11 | Linguaggio principale | Ecosistema AI maturo, tipizzazione moderna |
| FastAPI | ≥ 0.111 | Framework web / API REST | Asincrono, documentazione OpenAPI automatica |
| Docling (IBM) | ≥ 2.0 | Conversione PDF → Markdown | Unico tool che unisce OCR, analisi layout e riconoscimento tabelle in un solo pacchetto |
| Pydantic | v2 | Schema e validazione dati | Validazione runtime con errori leggibili, integrazione nativa con FastAPI e Outlines |
| Pandas | ≥ 2.0 | Normalizzazione e generazione CSV | Standard de-facto per la trasformazione di dati tabulari in Python |
| SQLAlchemy | ≥ 2.0 | Interfaccia con il database aziendale | ORM compatibile con SQLite e PostgreSQL senza cambiare codice |

### AI — 100% Locale

| Componente | Scelta | Perché |
|---|---|---|
| Runtime LLM | **Ollama** | Gira dentro Docker, gestisce GPU/CPU automaticamente, espone una semplice API REST |
| Modello | **`qwen2.5:3b`** | Miglior modello 3B per estrazione JSON strutturata; ~2GB RAM, ~20s/fattura su CPU |
| Structured output | **Outlines** | Vincola la generazione LLM alla grammatica Pydantic: impossibile ottenere JSON malformato |

**Perché `qwen2.5:3b` e non un modello più grande.**
Il task da svolgere è estrarre un insieme fisso di campi (tracking, importo, data, destinatario) da un documento testuale già convertito in Markdown. Non è un task che richiede ragionamento complesso. Un modello da 3 miliardi di parametri, correttamente istruito con un prompt few-shot, è più che sufficiente. La scelta permette di girare su qualsiasi hardware aziendale senza GPU dedicata, mantenendo tempi di elaborazione accettabili: ~20–30 secondi per fattura su CPU, ~3–5 secondi con GPU NVIDIA.

**Perché Outlines.**
Senza Outlines, l'LLM potrebbe produrre JSON parzialmente validi, testo extra fuori dalle parentesi graffe, o campi con tipi sbagliati. Outlines risolve il problema a monte, vincolando il processo di generazione dei token alla grammatica definita dallo schema Pydantic: il modello non può fisicamente produrre output non validi. Questo elimina la necessità di logiche di parsing difensive e retry per errori di formato.

### Frontend

L'interfaccia grafica è volutamente fuori scope per questa fase. Verrà implementata in seguito da un team dedicato. Per ora l'applicazione espone un'interfaccia minimale accessibile da browser — upload PDF, lista fatture processate, download CSV — basata su **Jinja2 + HTMX**, sufficiente per operare e testare il sistema senza dover usare direttamente le API.

### Storage

| Componente | Tecnologia | Note |
|---|---|---|
| PDF caricati e CSV generati | File system locale (volumi Docker) | Persistenti tra riavvii |
| Database applicativo | **SQLite** | Embedded, zero configurazione; migrabile a PostgreSQL senza modifiche al codice |
| Configurazioni trasportatori | File **YAML** | Modificabili senza toccare il codice sorgente |
| Modello AI | Volume Docker `ollama_models` | Scaricato una volta, persistente |

La scelta di SQLite in questa fase è deliberata: non richiede un container database separato, è sufficiente per i volumi previsti e permette di concentrarsi sulla pipeline senza overhead infrastrutturale. La struttura del codice tramite SQLAlchemy rende la migrazione a PostgreSQL una questione di una riga di configurazione.

---

## Architettura

Il codice è organizzato in tre livelli con responsabilità nettamente separate.

**Presentation Layer** — Endpoint FastAPI e template Jinja2. Riceve i PDF in upload, espone i risultati, gestisce il download dei CSV e l'avvio della riconciliazione. Non contiene logica di business.

**Service Layer** — Tutta la logica applicativa: parsing PDF, chiamata all'LLM, validazione, normalizzazione, riconciliazione con il database, generazione report. Ogni servizio è indipendente e testabile in isolamento.

**Integration Layer** — I client che parlano con i componenti Docker interni: Docling SDK, Ollama HTTP client, connettore SQLAlchemy. Questo strato isola il resto del codice dalle dipendenze infrastrutturali.

### Endpoint principali

| Metodo | Path | Funzione |
|---|---|---|
| `POST` | `/upload` | Carica un PDF fattura e avvia la pipeline |
| `GET` | `/invoices` | Lista fatture processate con stato |
| `GET` | `/invoices/{id}` | Dettaglio singola fattura estratta |
| `GET` | `/invoices/{id}/csv` | Download CSV normalizzato |
| `GET` | `/invoices/{id}/markdown` | Markdown intermedio prodotto da Docling (debug) |
| `POST` | `/invoices/{id}/reconcile` | Avvia confronto con DB aziendale |
| `GET` | `/reports/{id}` | Report discrepanze |
| `GET` | `/health` | Stato del sistema, modello caricato, GPU rilevata |

---

## Fasi di sviluppo

### Fase 1 — Analisi e Parsing PDF

**Obiettivo**: dato un qualsiasi PDF di fattura trasportatore, produrre un Markdown strutturato e fedele.

Utilizziamo **Docling** di IBM Research, che combina in un unico strumento analisi del layout (modello `DocLayNet`), riconoscimento struttura tabelle (modello `TableFormer`) e OCR (EasyOCR). I modelli interni di Docling sono leggeri (~150MB totali), ottimizzati per CPU e inclusi automaticamente nell'installazione del pacchetto — nessuna dipendenza aggiuntiva.

La pipeline rileva automaticamente se il PDF è testo nativo o una scansione: nel primo caso l'OCR è disabilitato (più veloce e preciso), nel secondo viene attivato con supporto italiano e inglese. Per PDF scansionati di qualità bassa (sbiaditi o inclinati), è previsto un pre-processing automatico con Pillow e OpenCV prima del passaggio a Docling.

Ogni trasportatore ha un proprio modulo parser, derivato da una classe base comune. I trasportatori non ancora configurati usano un parser generico. L'aggiunta di un nuovo trasportatore richiede un file YAML di configurazione e, se necessario, una classe parser dedicata. La selezione del parser corretto avviene tramite factory pattern, in base al mittente rilevato nel PDF.

Il Markdown prodotto da Docling viene sempre salvato su disco: è il dato intermedio più importante per il debug e per capire perché un'estrazione ha prodotto risultati imprecisi.

### Fase 2 — Integrazione AI ed Estrazione Dati

**Obiettivo**: trasformare il Markdown della fattura in un oggetto JSON strutturato e validato.

Il Markdown viene passato a **qwen2.5:3b** tramite il client HTTP di Ollama. Il prompt è composto da tre parti: definizione del ruolo (estrattore di dati da fatture di trasporto), schema JSON atteso, e due o tre esempi concreti (few-shot) tratti da fatture reali già annotate. Gli esempi few-shot sono la componente più critica del prompt e vengono raffinati progressivamente durante la fase di test.

**Outlines** è integrato come strato intermedio tra l'applicazione e Ollama: vincola il processo di sampling dei token alla grammatica definita dallo schema Pydantic. Il risultato è che il modello non può produrre altro che un JSON sintatticamente e strutturalmente valido. In caso di errore di comunicazione con Ollama (timeout, container non raggiungibile), il sistema solleva un'eccezione gestita e marca la fattura con stato di errore esplicito.

I prompt sono versionati nel codice come costanti con identificatore e data. Qualsiasi modifica al prompt viene trattata come una modifica al codice: deve essere testata sul set di fatture campione prima di essere integrata, per evitare regressioni silenziose sull'accuratezza.

### Fase 3 — Validazione e Normalizzazione

**Obiettivo**: garantire che i dati estratti siano corretti, completi e in formato uniforme prima di qualsiasi elaborazione successiva.

Il JSON prodotto dall'LLM viene deserializzato nello schema **Pydantic** corrispondente. Le regole di validazione includono: presenza dei campi obbligatori (tracking number, importo totale, data fattura), range ragionevoli per i valori numerici (importo tra 0 e 50.000 EUR, peso tra 0 e 1.000 kg), formato corretto per date e codici paese ISO 3166. I campi opzionali assenti nella fattura restano `null` e vengono gestiti esplicitamente nei passaggi successivi.

Le fatture che non superano la validazione non vengono scartate: vengono marcate come "da revisionare" e restano accessibili tramite interfaccia, con l'indicazione precisa del campo che ha causato il rifiuto. Questo permette una revisione manuale puntuale senza perdere il lavoro già svolto di parsing e conversione.

### Fase 4 — Trasformazione, Export CSV e Riconciliazione

**Obiettivo**: produrre CSV standardizzati e identificare le discrepanze con il database aziendale.

I dati validati vengono trasformati da **Pandas** in un formato CSV con colonne fisse, indipendenti dal trasportatore di origine. Il formato è documentato e concordato con il team che gestisce il database aziendale, per garantire l'importazione diretta senza trasformazioni aggiuntive da parte loro.

La riconciliazione confronta ogni riga del CSV con i record presenti nel database aziendale, usando il codice tracking come chiave primaria. Il confronto identifica tre categorie di discrepanze: costi fatturati diversi da quelli registrati a sistema (con delta in EUR), spedizioni presenti in fattura ma assenti nel database, spedizioni presenti nel database ma assenti in fattura. Il report viene generato in formato CSV per elaborazione programmatica e in una vista HTML per la consultazione diretta.

### Fase 5 — Testing e Validazione

**Obiettivo**: misurare l'accuratezza della pipeline su dati reali e garantire la stabilità nel tempo.

La strategia di test prevede tre livelli. I **test unitari** coprono i singoli componenti (validatori Pydantic, normalizzatori, logica di riconciliazione) in isolamento completo. I **test di integrazione** eseguono l'intera pipeline su un set di PDF campione già annotati manualmente, misurando l'accuratezza campo per campo. I **test di regressione** vengono eseguiti ad ogni modifica al prompt o alla pipeline, usando lo stesso set campione, per rilevare deterioramenti nell'estrazione prima che raggiungano la produzione.

L'accuratezza target per il rilascio è ≥ 95% sui campi obbligatori (tracking number, importo totale, data fattura) e ≥ 85% sui campi opzionali (tipo servizio, peso, destinatario). Le fatture che scendono sotto una soglia di confidence interna vengono automaticamente indirizzate alla coda di revisione manuale.

---

## Struttura del repository

```
fatturazione-passiva/
│
├── app/
│   ├── main.py                  # Entry point FastAPI
│   ├── parsers/                 # Docling + parser per trasportatore
│   ├── extractors/              # Client Ollama + integrazione Outlines
│   ├── models/                  # Schema Pydantic (fattura, spedizione, report)
│   ├── services/                # Logica riconciliazione, CSV export, report
│   ├── routers/                 # Definizione endpoint FastAPI
│   └── templates/               # Template Jinja2 per UI browser
│
├── config/
│   └── carriers/                # File YAML di configurazione per trasportatore
│
├── uploads/                     # PDF caricati (volume Docker)
├── outputs/                     # CSV e report generati (volume Docker)
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/                # PDF campione annotati per i test
│
├── docs/
│   └── layouts/                 # Documentazione layout PDF per trasportatore
│
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## Roadmap

| Milestone | Attività | Settimana |
|---|---|---|
| **M1** — Ambiente e Parsing | Setup Docker Compose completo, test Docling su PDF reali dei trasportatori in uso, documentazione layout, raccolta 10+ PDF campione per trasportatore | 1–2 |
| **M2** — Estrazione AI | Implementazione client Ollama + Outlines, ingegneria del prompt con few-shot examples, validazione Pydantic, test di accuratezza su 30+ PDF reali | 3–4 |
| **M3** — Robustezza e Logging | Gestione timeout e errori Ollama, edge case PDF (scansioni sbiadite, layout anomali), logging strutturato dell'intera pipeline per ogni fattura | 5 |
| **M4** — CSV e Riconciliazione | Normalizzatore completo, generazione CSV con formato concordato con il team DB, logica di confronto, generazione report discrepanze | 6 |
| **M5** — Stabilizzazione e Rilascio | Suite di test completa con metriche di accuratezza, endpoint FastAPI documentati, UI browser funzionante, runbook operativo | 7–8 |

---

## Rischi e mitigazioni

| Rischio | Mitigazione |
|---|---|
| PDF scansionati di qualità bassa | Pre-processing automatico con OpenCV (deskew, denoise) prima di Docling |
| LLM produce valori numerici errati | Validazione Pydantic con range attesi + Outlines che rende impossibile JSON malformato |
| Docling non riconosce correttamente le tabelle | Il Markdown intermedio è sempre salvato su disco: permette di diagnosticare e correggere la pipeline senza riprocessare il PDF da capo |
| Cambio layout fattura da parte del trasportatore | Monitoraggio accuracy su set campione mensile; alert automatico se scende sotto soglia |
| Modifiche al prompt che causano regressioni silenziose | Prompt versionati nel codice; ogni modifica passa dai test di regressione prima del merge |
| Hardware con RAM insufficiente | `qwen2.5:3b` richiede ~4GB, Docling ~2GB: totale ~6GB. Requisito minimo dichiarato: 8GB RAM |

---

## Decisioni architetturali

| Decisione | Alternativa considerata | Motivazione |
|---|---|---|
| Docker Compose come unico prerequisito | Installazione manuale (Python, Ollama, dipendenze) | L'utente esegue un comando solo; zero setup manuale; ambiente riproducibile su qualsiasi macchina |
| `qwen2.5:3b` come modello fisso | Modelli 7B+ o API cloud (Gemini, Claude) | Sufficiente per il task, gira su CPU senza GPU, ~2GB, compatibile con qualsiasi hardware aziendale |
| Outlines per structured output | Parsing regex o retry su JSON invalidi | Elimina per costruzione la categoria di errori "JSON malformato", non la gestisce a posteriori |
| Docling per il parsing PDF | Tesseract standalone o PyMuPDF | Unico strumento che combina OCR, layout analysis e table detection in una singola libreria Python |
| SQLite come database applicativo | PostgreSQL da subito | Zero configurazione aggiuntiva; sufficiente per il volume previsto; migrazione a Postgres è una riga di configurazione |
| Prompt few-shot versionati nel codice | Prompt configurabili esternamente | Tracciabilità completa; le regressioni sono identificabili tramite diff; nessun disallineamento tra codice e comportamento |