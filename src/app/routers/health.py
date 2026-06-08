import httpx
from fastapi import APIRouter
from pydantic import BaseModel
import os

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    ollama_reachable: bool
    model_loaded: bool
    gpu_detected: bool


@router.get("/health", response_model=HealthResponse)
async def health_check():
    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    model_name = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
    
    ollama_reachable = False
    model_loaded = False
    gpu_detected = False

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{ollama_url}/api/tags")
            if resp.status_code == 200:
                ollama_reachable = True
                tags = resp.json().get("models", [])
                model_loaded = any(t["name"] == model_name or t["name"].startswith(model_name + ":") for t in tags)
            
            # Verifica GPU
            gpu_resp = await client.get(f"{ollama_url}/api/ps")
            if gpu_resp.status_code == 200:
                ps_data = gpu_resp.json()
                gpu_detected = any(
                    m.get("size_vram", 0) > 0
                    for m in ps_data.get("models", [])
                )
    except httpx.RequestError:
        pass

    return HealthResponse(
        status="ok" if (ollama_reachable and model_loaded) else "degraded",
        ollama_reachable=ollama_reachable,
        model_loaded=model_loaded,
        gpu_detected=gpu_detected,
    )