from __future__ import annotations
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict


class Party(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    ruc: Optional[str] = ""
    nombre: Optional[str] = ""
    direccion: Optional[str] = ""
    telefono: Optional[str] = ""
    email: Optional[str] = ""


class Totales(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    exentas: float = 0.0
    gravado_5: float = 0.0
    iva_5: float = 0.0
    gravado_10: float = 0.0
    iva_10: float = 0.0
    total: float = 0.0


class InvoiceHeader(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str
    cdc: Optional[str] = ""

    # Documento
    tipo_documento: Optional[str] = "CO"  # CO/CR
    establecimiento: Optional[str] = ""
    punto: Optional[str] = ""
    numero: Optional[str] = ""
    numero_documento: Optional[str] = ""
    fecha_emision: Optional[datetime] = None
    condicion_venta: Optional[str] = "CONTADO"

    # Moneda
    moneda: Optional[str] = "GS"
    tipo_cambio: float = 0.0

    # Identificadores
    timbrado: Optional[str] = ""

    # Partes
    emisor: Optional[Party] = None
    receptor: Optional[Party] = None

    # Totales
    totales: Totales = Field(default_factory=Totales)

    # Metadata / Índices
    email_origen: Optional[str] = ""
    mes_proceso: Optional[str] = ""
    fuente: Optional[str] = ""  # XML_NATIVO / OPENAI_VISION
    owner_email: Optional[str] = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class InvoiceDetail(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    header_id: str
    linea: int
    descripcion: str
    cantidad: float
    unidad: Optional[str] = ""
    precio_unitario: float
    total: float
    iva: int = 0  # 0, 5, 10
    owner_email: Optional[str] = ""


class InvoiceDocument(BaseModel):
    header: InvoiceHeader
    items: List[InvoiceDetail]
