from __future__ import annotations
from typing import Tuple, List, Optional
from datetime import datetime

from app.models.models import InvoiceData
from app.models.invoice_v2 import InvoiceHeader, InvoiceDetail, InvoiceDocument, Party, Totales


def _split_numero(numero: Optional[str]) -> Tuple[str, str, str, str]:
    if not numero:
        return "", "", "", ""
    parts = str(numero).split("-")
    if len(parts) == 3:
        est, pto, num = parts
        return est, pto, num, f"{est}-{pto}-{num}"
    return "", "", numero, numero


def map_invoice(invoice: InvoiceData, fuente: str = "") -> InvoiceDocument:
    est, pto, num, full = _split_numero(getattr(invoice, "numero_factura", ""))
    fecha: Optional[datetime] = getattr(invoice, "fecha", None)
    if fecha and isinstance(fecha, str):
        try:
            fecha = datetime.fromisoformat(fecha)
        except Exception:
            fecha = None

    header_id = (getattr(invoice, "cdc", None) or f"{getattr(invoice, 'ruc_emisor', 'SINRUC')}_{full}_{(fecha or datetime.utcnow()).date().isoformat()}")

    header = InvoiceHeader(
        id=str(header_id),
        cdc=getattr(invoice, "cdc", ""),
        tipo_documento=getattr(invoice, "tipo_documento", "CO") or "CO",
        establecimiento=est,
        punto=pto,
        numero=num,
        numero_documento=full,
        fecha_emision=fecha,
        condicion_venta=(getattr(invoice, "condicion_venta", "CONTADO") or "CONTADO").upper(),
        moneda=(getattr(invoice, "moneda", "GS") or "GS").upper(),
        tipo_cambio=float(getattr(invoice, "tipo_cambio", 0.0) or 0.0),
        timbrado=getattr(invoice, "timbrado", ""),
        emisor=Party(
            ruc=getattr(invoice, "ruc_emisor", ""),
            nombre=getattr(invoice, "nombre_emisor", "")
        ),
        receptor=Party(
            ruc=getattr(invoice, "ruc_cliente", ""),
            nombre=getattr(invoice, "nombre_cliente", ""),
            email=getattr(invoice, "email_cliente", "")
        ),
        totales=Totales(
            exentas=float(getattr(invoice, "subtotal_exentas", 0) or 0),
            gravado_5=float(getattr(invoice, "subtotal_5", 0) or 0),
            iva_5=float(getattr(invoice, "iva_5", 0) or 0),
            gravado_10=float(getattr(invoice, "subtotal_10", 0) or 0),
            iva_10=float(getattr(invoice, "iva_10", 0) or 0),
            total=float(getattr(invoice, "monto_total", 0) or 0)
        ),
        email_origen=getattr(invoice, "email_origen", ""),
        mes_proceso=getattr(invoice, "mes_proceso", ""),
        fuente=fuente
    )

    items: List[InvoiceDetail] = []
    productos = getattr(invoice, "productos", []) or []
    for idx, p in enumerate(productos, start=1):
        if isinstance(p, dict):
            desc = p.get("articulo", "")
            cant = float(p.get("cantidad", 0) or 0)
            pu = float(p.get("precio_unitario", 0) or 0)
            tot = float(p.get("total", 0) or 0)
            iva = int(p.get("iva", 0) or 0)
        else:
            desc = getattr(p, "articulo", "") or ""
            cant = float(getattr(p, "cantidad", 0) or 0)
            pu = float(getattr(p, "precio_unitario", 0) or 0)
            tot = float(getattr(p, "total", 0) or 0)
            iva = int(getattr(p, "iva", 0) or 0)
        items.append(InvoiceDetail(
            header_id=header.id,
            linea=idx,
            descripcion=desc,
            cantidad=cant,
            precio_unitario=pu,
            total=tot,
            iva=iva
        ))

    return InvoiceDocument(header=header, items=items)

