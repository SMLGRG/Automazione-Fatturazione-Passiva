# src/app/models/__init__.py
from __future__ import annotations
from datetime import date
from decimal import Decimal
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, Any
import re


class Shipment(BaseModel):
    model_config = {"populate_by_name": True}

    tracking_number: Optional[str] = Field(default=None)
    shipment_date: Optional[str] = Field(default=None)          # data della spedizione (non della fattura)
    sender_name: Optional[str] = Field(default=None)            # MITTENTE
    recipient_name: Optional[str] = Field(default=None)         # DESTINATARIO
    destination_city: Optional[str] = Field(default=None)       # DESTINAZIONE (città)
    destination_postal_code: Optional[str] = Field(default=None) # CAP
    destination_country: Optional[str] = Field(default=None)    # paese del destinatario
    weight_kg: Optional[str] = Field(default=None)
    service_type: Optional[str] = Field(default=None)
    total_amount: Optional[str] = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def remap_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Retrocompatibilità: accetta invoice_date o date come shipment_date
            if "shipment_date" not in data:
                data["shipment_date"] = (
                    data.pop("invoice_date", None)
                    or data.pop("shipment_date", None)
                    or data.pop("date_of_invoice", None)
                    or data.pop("date", None)
                )
            if "total_amount" not in data:
                data["total_amount"] = (
                    data.pop("freight_charges", None)
                    or data.pop("total_amount_due", None)
                )
        return data


class InvoiceExtraction(BaseModel):
    model_config = {"populate_by_name": True}

    carrier_name: str = Field(default="unknown")
    invoice_number: str = Field(default="unknown")
    invoice_date: Optional[str] = Field(default=None)
    total_invoice_amount: Optional[str] = Field(default=None)
    shipments: list["Shipment"] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def remap_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "invoice_date" not in data:
                data["invoice_date"] = (
                    data.pop("date_of_invoice", None)
                    or data.pop("shipment_date", None)
                )
            if "total_invoice_amount" not in data:
                data["total_invoice_amount"] = (
                    data.pop("total_amount_due", None)
                    or data.pop("total_amount", None)
                )
        return data


class InvoiceStatus(BaseModel):
    """Stato di elaborazione di una fattura nel sistema."""

    id: int
    filename: str
    status: str  # "processing" | "completed" | "review_required" | "error"
    error_field: Optional[str] = None
    error_message: Optional[str] = None
    extraction: Optional[InvoiceExtraction] = None