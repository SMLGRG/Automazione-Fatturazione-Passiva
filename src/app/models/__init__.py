from __future__ import annotations
from pydantic import BaseModel, Field, model_validator
from typing import Optional, Any


class Shipment(BaseModel):
    """
    Dati di una singola spedizione estratti dalla fattura.

    Campi core (gli 8 richiesti):
      tracking_number, shipment_date, sender_name, recipient_name,
      destination_city, destination_postal_code, weight_kg, total_amount

    Qualsiasi altro campo (destination_country, service_type, orario_consegna...)
    viene accettato e preservato via extra="allow". Appare automaticamente nel CSV.
    """
    model_config = {"extra": "allow"}

    tracking_number: Optional[str] = Field(default=None)
    shipment_date: Optional[str] = Field(default=None)
    sender_name: Optional[str] = Field(default=None)
    recipient_name: Optional[str] = Field(default=None)
    destination_city: Optional[str] = Field(default=None)
    destination_postal_code: Optional[str] = Field(default=None)
    weight_kg: Optional[float] = Field(default=None)
    total_amount: Optional[float] = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def remap_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "shipment_date" not in data:
                data["shipment_date"] = (
                    data.pop("invoice_date", None)
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
    carrier_name: str = Field(default="unknown")
    invoice_number: str = Field(default="unknown")
    invoice_date: Optional[str] = Field(default=None)
    total_invoice_amount: Optional[float] = Field(default=None)
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
    id: int
    filename: str
    status: str  # "processing" | "awaiting_sample" | "completed" | "review_required" | "error"
    error_field: Optional[str] = None
    error_message: Optional[str] = None
    extraction: Optional[InvoiceExtraction] = None


class FieldInput(BaseModel):
    """Valore di un campo fornito dall'utente nel campione."""
    value: str
    description: str = ""
    hint: str = ""


class TemplateField(BaseModel):
    """Definizione di un campo nel template salvato su disco."""
    name: str
    description: str
    example: str
    hint: str = ""


class TemplateConfig(BaseModel):
    """Struttura completa del template JSON salvato in config/templates/."""
    carrier_name: str
    version: int = 1
    created_at: str = ""
    shipment_splitter: str
    batch_size: int = 8
    extraction_block_chars: int = 1000
    fields: list[TemplateField] = Field(default_factory=list)
    extraction_prompt: str = ""


class CarrierSample(BaseModel):
    """
    Campione utente unificato. Sostituisce CarrierSample + CarrierSampleV2.

    Accetta due formati per ogni campo, in modo trasparente:

      Formato semplice (stringa):
        {"tracking_number": "01/23/500072", "sender_name": "MARCHI & FILDI"}

      Formato ricco (con contesto):
        {"tracking_number": {"value": "01/23/500072", "description": "...", "hint": "..."}}

      Misto (un campo semplice, uno ricco):
        {"tracking_number": "01/23/500072", "sender_name": {"value": "MARCHI & FILDI", ...}}

    Gli 8 campi core sono dichiarati esplicitamente.
    Qualsiasi campo custom (destination_country, orario_consegna, ecc.) viene
    accettato via extra="allow" e incluso automaticamente nel template generato.
    """
    model_config = {"extra": "allow"}

    tracking_number: Optional[FieldInput] = None
    shipment_date: Optional[FieldInput] = None
    sender_name: Optional[FieldInput] = None
    recipient_name: Optional[FieldInput] = None
    destination_city: Optional[FieldInput] = None
    destination_postal_code: Optional[FieldInput] = None
    weight_kg: Optional[FieldInput] = None
    total_amount: Optional[FieldInput] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_all_fields(cls, data: Any) -> Any:
        """
        Normalizza tutti i campi (noti ed extra) in FieldInput.
        - Stringa → FieldInput(value=stringa, description="", hint="")
        - Dict con "value" → FieldInput(**dict)
        - Già FieldInput → invariato
        """
        if not isinstance(data, dict):
            return data
        for key, value in list(data.items()):
            if value is None:
                continue
            if isinstance(value, str):
                # Formato semplice: promuovi a FieldInput
                data[key] = FieldInput(
                    value=value,
                    description=key.replace("_", " "),
                    hint="",
                )
            elif isinstance(value, dict) and "value" in value:
                # Formato ricco: valida come FieldInput
                try:
                    data[key] = FieldInput(**value)
                except Exception:
                    pass
            # Se è già un FieldInput (es. re-validation) lo lascia invariato
        return data