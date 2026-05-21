# app/main.py — versione minimale M1
from fastapi import FastAPI

app = FastAPI(
    title="Automazione Fatturazione Passiva",
    description="Pipeline locale PDF → CSV riconciliato",
    version="0.1.0",
)

@app.get("/health")
def health():
    return {"status": "ok"}