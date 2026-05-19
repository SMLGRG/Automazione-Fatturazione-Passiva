#!/bin/bash
set -e
echo "=== Fatturazione Passiva — Avvio ==="

command -v python3 >/dev/null 2>&1 || { echo "[ERRORE] Python non trovato. Installa da: https://www.python.org/downloads/"; exit 1; }
command -v ollama >/dev/null 2>&1 || { echo "[ERRORE] Ollama non trovato. Installa da: https://ollama.com/download"; exit 1; }

[ ! -d ".venv" ] && echo "[1/3] Creazione ambiente Python..." && python3 -m venv .venv

echo "[2/3] Installazione dipendenze..."
source .venv/bin/activate
pip install -r src/requirements.txt -q

echo "[3/3] Verifica modello AI..."
ollama pull phi4-mini

echo ""
echo "Applicazione disponibile su: http://localhost:8000"
echo "Premi Ctrl+C per fermare."
open "http://localhost:8000" 2>/dev/null || xdg-open "http://localhost:8000" 2>/dev/null || true

cd src
uvicorn app.main:app --host 0.0.0.0 --port 8000