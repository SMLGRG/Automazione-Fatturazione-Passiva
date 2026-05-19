@echo off
chcp 65001 >nul
echo === Fatturazione Passiva — Avvio ===

REM Controlla Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERRORE] Python non trovato.
    echo Scaricalo da: https://www.python.org/downloads/
    echo Assicurati di spuntare "Add Python to PATH" durante l'installazione.
    pause
    exit /b 1
)

REM Controlla Ollama
ollama --version >nul 2>&1
if errorlevel 1 (
    echo [ERRORE] Ollama non trovato.
    echo Scaricalo da: https://ollama.com/download
    pause
    exit /b 1
)

REM Crea venv se non esiste (lo creiamo nella root principale)
if not exist ".venv" (
    echo [1/3] Creazione ambiente Python isolato...
    python -m venv .venv
)

REM Attiva venv e installa dipendenze (andando a cercare requirements.txt dentro src)
echo [2/3] Installazione dipendenze...
call .venv\Scripts\activate.bat
pip install -r src\requirements.txt -q

REM Scarica il modello se non presente
echo [3/3] Verifica modello AI...
ollama pull phi4-mini

REM Avvia puntando alla cartella src
echo.
echo Applicazione disponibile su: http://localhost:8000
echo Premi Ctrl+C per fermare.
echo.
start "" http://localhost:8000

REM Spostiamo temporaneamente l'esecuzione dentro src così FastAPI trova tutto correttamente
cd src
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000