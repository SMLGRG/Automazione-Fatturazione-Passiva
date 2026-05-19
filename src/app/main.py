# src/app/main.py
import logging
from fastapi import FastAPI, HTTPException
import httpx

# Configurazione del logging di base per vedere cosa succede nel terminale
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Automazione Fatturazione Passiva",
    description="API per il parsing di fatture e l'estrazione dati tramite AI",
    version="1.0.0"
)

# URL di default per connettersi a Ollama locale
OLLAMA_URL = "http://localhost:11434"

@app.get("/")
def read_root():
    """Endpoint di benvenuto."""
    return {
        "message": "Benvenuto nel sistema di Automazione Fatturazione Passiva API",
        "docs": "/docs"
    }

@app.get("/health")
async def health_check():
    """
    Verifica lo stato di salute dell'applicazione e controlla
    se Ollama è attivo e raggiungibile in locale.
    """
    ollama_reachable = False
    model_loaded = False
    
    async with httpx.AsyncClient() as client:
        try:
            # 1. Controlla se il server Ollama risponde
            response = await client.get(OLLAMA_URL, timeout=2.0)
            if response.status_code == 200:
                ollama_reachable = True
                
                # 2. Controlla se il modello richiesto (phi4-mini) è presente locale
                tags_response = await client.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
                if tags_response.status_code == 200:
                    models_list = tags_response.json().get("models", [])
                    # Controlla se almeno un modello contiene 'phi4-mini' nel nome
                    for model in models_list:
                        if "phi4-mini" in model.get("name", ""):
                            model_loaded = True
                            break
        except httpx.RequestError:
            # Ollama non è avviato o la porta è occupata
            logger.warning("Impossibile connettersi a Ollama. Assicurati che sia in esecuzione.")

    return {
        "status": "ok",
        "ollama_reachable": ollama_reachable,
        "model_loaded": model_loaded,
        "gpu_detected": False  # Verrà implementato in seguito nelle fasi successive
    }