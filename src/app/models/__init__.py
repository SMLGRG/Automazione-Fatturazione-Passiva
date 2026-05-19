# src/app/models/__init__.py
from __future__ import annotations
from datetime import date
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, Field, field_validator
import re


class Shipment(BaseModel):
    """Singola spedizione estratta da una fattura."""
    
    tracking_number: str = Field(
        description="Codice tracking univoco della spedizione"
    )
    invoice_date: date = Field(
        description="Data della fattura nel formato YYYY-MM-DD"
    )
    total_amount: Decimal = Field(
        ge=Decimal("0"),
        le=Decimal("50000"),
        description="Importo totale fatturato in EUR"
    )
    recipient_name: Optional[str] = Field(
        default=None,
        description="Nome del destinatario della spedizione"
    )
    destination_country: Optional[str] = Field(
        default=None,
        description="Codice paese ISO 3166-1 alpha-2 (es. IT, DE, FR)"
    )
    weight_kg: Optional[Decimal] = Field(
        default=None,
        ge=Decimal("0"),
        le=Decimal("1000"),
        description="Peso in kg"
    )
    service_type: Optional[str] = Field(
        default=None,
        description="Tipo di servizio (es. EXPRESS, STANDARD, ECONOMY)"
    )

    @field_validator("tracking_number")
    @classmethod
    def tracking_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Il tracking number non può essere vuoto")
        return v.strip().upper()

    @field_validator("destination_country")
    @classmethod
    def validate_country_code(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not re.match(r"^[A-Z]{2}$", v.upper()):
            raise ValueError(f"Codice paese non valido: {v}. Atteso formato ISO 3166 (es. IT)")
        return v.upper()


class InvoiceExtraction(BaseModel):
    """Dati completi estratti da una fattura trasportatore."""
    
    carrier_name: str = Field(description="Nome del trasportatore")
    invoice_number: str = Field(description="Numero della fattura")
    invoice_date: date = Field(description="Data emissione fattura")
    shipments: list[Shipment] = Field(
        min_length=1,
        description="Lista delle spedizioni nella fattura"
    )
    total_invoice_amount: Decimal = Field(
        ge=Decimal("0"),
        le=Decimal("50000"),
        description="Totale complessivo della fattura in EUR"
    )


class InvoiceStatus(BaseModel):
    """Stato di elaborazione di una fattura nel sistema."""
    
    id: int
    filename: str
    status: str  # "processing" | "completed" | "review_required" | "error"
    error_field: Optional[str] = None
    error_message: Optional[str] = None
    extraction: Optional[InvoiceExtraction] = None