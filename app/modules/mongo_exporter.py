"""
Almacenamiento en MongoDB de facturas procesadas (exportador documental).
Separado del módulo de Excel para cumplir con la eliminación total de Excel.
"""

import logging
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from decimal import Decimal
import threading

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError
import asyncio

from app.models.models import InvoiceData
from app.config.settings import settings

logger = logging.getLogger(__name__)
_MONGO_LOCK = threading.Lock()


class MongoDBExporter:
    def __init__(self,
                 connection_string: Optional[str] = None,
                 database_name: str = "cuenlyapp_warehouse",
                 collection_name: str = "facturas_completas") -> None:

        self.connection_string = connection_string or self._get_default_connection()
        self.database_name = database_name
        self.collection_name = collection_name

        self._sync_client: Optional[MongoClient] = None
        self._async_client: Optional[AsyncIOMotorClient] = None

        logger.info("MongoDBExporter configurado: db=%s, collection=%s",
                    database_name, collection_name)

    def _get_default_connection(self) -> str:
        mongo_url = getattr(settings, 'MONGODB_URL', None)
        if mongo_url:
            return mongo_url
        return "mongodb://localhost:27017/"

    def _get_sync_client(self) -> MongoClient:
        if not self._sync_client:
            try:
                self._sync_client = MongoClient(
                    self.connection_string,
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=10000,
                    socketTimeoutMS=20000,
                    maxPoolSize=50,
                    minPoolSize=5
                )
                self._sync_client.admin.command('ping')
                logger.info("✅ Conexión MongoDB establecida (sync)")
            except Exception as e:
                logger.error("❌ Error conectando a MongoDB: %s", e)
                raise
        return self._sync_client

    async def _get_async_client(self) -> AsyncIOMotorClient:
        if not self._async_client:
            try:
                self._async_client = AsyncIOMotorClient(
                    self.connection_string,
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=10000,
                    socketTimeoutMS=20000,
                    maxPoolSize=50,
                    minPoolSize=5
                )
                await self._async_client.admin.command('ping')
                logger.info("✅ Conexión MongoDB async establecida")
            except Exception as e:
                logger.error("❌ Error conectando a MongoDB async: %s", e)
                raise
        return self._async_client

    def export_invoices(self, invoices: List[InvoiceData]) -> Dict[str, Any]:
        if not invoices:
            logger.warning("No hay facturas para exportar a MongoDB")
            return {"inserted": 0, "updated": 0, "errors": 0, "total": 0}

        try:
            with _MONGO_LOCK:
                client = self._get_sync_client()
                db = client[self.database_name]
                collection = db[self.collection_name]

                self._ensure_indexes(collection)

                documents = []
                for inv in invoices:
                    doc = self._invoice_to_document(inv)
                    documents.append(doc)

                result = self._bulk_upsert(collection, documents)
                logger.info("📊 MongoDB Export: %d insertados, %d actualizados de %d facturas",
                            result["inserted"], result["updated"], len(invoices))
                return result

        except Exception as e:
            logger.error("Error exportando a MongoDB: %s", e, exc_info=True)
            return {"inserted": 0, "updated": 0, "errors": len(invoices), "total": len(invoices)}

    async def export_invoices_async(self, invoices: List[InvoiceData]) -> Dict[str, Any]:
        if not invoices:
            logger.warning("No hay facturas para exportar a MongoDB (async)")
            return {"inserted": 0, "updated": 0, "errors": 0, "total": 0}

        try:
            client = await self._get_async_client()
            db = client[self.database_name]
            collection = db[self.collection_name]

            await self._ensure_indexes_async(collection)

            documents = []
            for inv in invoices:
                doc = self._invoice_to_document(inv)
                documents.append(doc)

            result = await self._bulk_upsert_async(collection, documents)
            logger.info("📊 MongoDB Export Async: %d insertados, %d actualizados de %d facturas",
                        result["inserted"], result["updated"], len(invoices))
            return result

        except Exception as e:
            logger.error("Error exportando a MongoDB async: %s", e, exc_info=True)
            return {"inserted": 0, "updated": 0, "errors": len(invoices), "total": len(invoices)}

    def close_connections(self) -> None:
        try:
            if self._sync_client:
                self._sync_client.close()
                self._sync_client = None
        except Exception:
            pass
        try:
            if self._async_client:
                self._async_client.close()
                self._async_client = None
        except Exception:
            pass

    # -------------- Internos --------------

    def _ensure_indexes(self, collection) -> None:
        try:
            collection.create_index({"factura.fecha": 1})
            collection.create_index({"emisor.ruc": 1})
            collection.create_index({"receptor.ruc": 1})
            collection.create_index({"metadata.fecha_procesado": 1})
            collection.create_index({"indices.year_month": 1})
            collection.create_index({"datos_tecnicos.cdc": 1})
        except PyMongoError as e:
            logger.warning("No se pudieron asegurar índices: %s", e)

    async def _ensure_indexes_async(self, collection) -> None:
        try:
            await collection.create_index({"factura.fecha": 1})
            await collection.create_index({"emisor.ruc": 1})
            await collection.create_index({"receptor.ruc": 1})
            await collection.create_index({"metadata.fecha_procesado": 1})
            await collection.create_index({"indices.year_month": 1})
            await collection.create_index({"datos_tecnicos.cdc": 1})
        except PyMongoError as e:
            logger.warning("No se pudieron asegurar índices: %s", e)

    def _bulk_upsert(self, collection, documents: List[Dict[str, Any]]) -> Dict[str, Any]:
        inserted = 0
        updated = 0
        errors = 0
        for doc in documents:
            try:
                _id = doc.get("_id")
                res = collection.replace_one({"_id": _id}, doc, upsert=True)
                if res.upserted_id is not None:
                    inserted += 1
                elif res.modified_count > 0:
                    updated += 1
            except PyMongoError:
                errors += 1
        return {"inserted": inserted, "updated": updated, "errors": errors, "total": len(documents)}

    async def _bulk_upsert_async(self, collection, documents: List[Dict[str, Any]]) -> Dict[str, Any]:
        inserted = 0
        updated = 0
        errors = 0
        for doc in documents:
            try:
                _id = doc.get("_id")
                res = await collection.replace_one({"_id": _id}, doc, upsert=True)
                if getattr(res, 'upserted_id', None) is not None:
                    inserted += 1
                elif res.modified_count and res.modified_count > 0:
                    updated += 1
            except PyMongoError:
                errors += 1
        return {"inserted": inserted, "updated": updated, "errors": errors, "total": len(documents)}

    def _invoice_to_document(self, invoice: InvoiceData) -> Dict[str, Any]:
        fecha_factura = invoice.fecha.isoformat() if invoice.fecha else None
        fecha_procesado = getattr(invoice, 'procesado_en', datetime.now(timezone.utc)).isoformat()

        unique_id = f"{invoice.ruc_emisor or 'SIN_RUC'}_{invoice.numero_factura or 'SIN_NUM'}_{fecha_factura or 'SIN_FECHA'}"

        doc = {
            "_id": unique_id,
            "factura_id": unique_id,
            "metadata": {
                "fecha_procesado": fecha_procesado,
                "fuente": "XML_NATIVO" if getattr(invoice, "cdc", "") else "OPENAI_VISION",
                "calidad_datos": self._evaluar_calidad_datos(invoice),
                "version_esquema": "1.0",
                "email_origen": getattr(invoice, "email_origen", ""),
                "mes_proceso": getattr(invoice, "mes_proceso", ""),
                "pdf_path": getattr(invoice, "pdf_path", "")
            },
            "factura": {
                "numero": invoice.numero_factura or "",
                "fecha": fecha_factura,
                "tipo_documento": getattr(invoice, "tipo_documento", "CO"),
                "moneda": getattr(invoice, "moneda", "GS"),
                "tipo_cambio": float(getattr(invoice, "tipo_cambio", 1.0) or 1.0),
                "condicion_venta": getattr(invoice, "condicion_venta", "CONTADO"),
                "condicion_compra": getattr(invoice, "condicion_compra", "CONTADO"),
                "descripcion": getattr(invoice, "descripcion_factura", ""),
                "observacion": getattr(invoice, "observacion", "")
            },
            "emisor": {
                "ruc": invoice.ruc_emisor or "",
                "nombre": invoice.nombre_emisor or "",
                "actividad_economica": getattr(invoice, "actividad_economica", "")
            },
            "receptor": {
                "ruc": getattr(invoice, "ruc_cliente", ""),
                "nombre": getattr(invoice, "nombre_cliente", ""),
                "email": getattr(invoice, "email_cliente", "")
            },
            "montos": {
                "monto_total": float(getattr(invoice, "monto_total", 0) or 0),
                "subtotal_exentas": float(getattr(invoice, "subtotal_exentas", 0) or 0),
                "subtotal_5": float(getattr(invoice, "subtotal_5", 0) or 0),
                "subtotal_10": float(getattr(invoice, "subtotal_10", 0) or 0),
                "iva_5": float(getattr(invoice, "iva_5", 0) or 0),
                "iva_10": float(getattr(invoice, "iva_10", 0) or 0),
                "total_iva": float(getattr(invoice, "iva", 0) or 0)
            },
            "productos": [self._producto_to_doc(p) for p in (getattr(invoice, "productos", []) or [])],
            "datos_tecnicos": {
                "timbrado": getattr(invoice, "timbrado", ""),
                "cdc": getattr(invoice, "cdc", "")
            },
            "indices": {
                "year_month": getattr(invoice, "mes_proceso", ""),
                "has_cdc": bool(getattr(invoice, "cdc", ""))
            }
        }
        return doc

    def _producto_to_doc(self, p: Any) -> Dict[str, Any]:
        try:
            articulo = (p.get("articulo") if isinstance(p, dict) else getattr(p, "articulo", "")) or ""
            cantidad = float((p.get("cantidad") if isinstance(p, dict) else getattr(p, "cantidad", 0)) or 0)
            precio_u = float((p.get("precio_unitario") if isinstance(p, dict) else getattr(p, "precio_unitario", 0)) or 0)
            total = float((p.get("total") if isinstance(p, dict) else getattr(p, "total", 0)) or 0)
        except Exception:
            articulo = str(p)
            cantidad = 0
            precio_u = 0
            total = 0
        return {
            "articulo": articulo,
            "cantidad": cantidad,
            "precio_unitario": precio_u,
            "total": total
        }

    def _evaluar_calidad_datos(self, inv: InvoiceData) -> str:
        score = 0
        try:
            if getattr(inv, "cdc", ""): score += 2
            if getattr(inv, "timbrado", ""): score += 1
            if getattr(inv, "productos", None): score += 1
            if getattr(inv, "monto_total", 0) and getattr(inv, "fecha", None): score += 1
        except Exception:
            pass
        if score >= 4:
            return "ALTA"
        if score >= 2:
            return "MEDIA"
        return "BAJA"

