# app/main.py
import logging
import logging.config
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from app.routers import invoices, reports, health

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {
            "format": "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "structured",
        },
        "file": {
            "class": "logging.FileHandler",
            "filename": "outputs/pipeline.log",
            "formatter": "structured",
            "encoding": "utf-8",
        },
    },
    "root": {
        "level": "INFO",
        "handlers": ["console", "file"],
    },
}

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Applicazione avviata — pipeline pronta")
    yield
    logger.info("Applicazione in chiusura")


app = FastAPI(
    title="Automazione Fatturazione Passiva",
    description="Pipeline locale PDF → CSV riconciliato",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(invoices.router)
app.include_router(reports.router)
app.include_router(health.router)

templates = Jinja2Templates(directory="app/templates")

