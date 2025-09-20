from __future__ import annotations
import logging
from typing import List, Optional
from datetime import datetime

from pymongo import MongoClient, UpdateOne
from pymongo.collection import Collection

from app.models.invoice_v2 import InvoiceHeader, InvoiceDetail, InvoiceDocument
from app.repositories.invoice_repository import InvoiceRepository
from app.config.settings import settings

logger = logging.getLogger(__name__)


class MongoInvoiceRepository(InvoiceRepository):
    def __init__(self,
                 connection_string: Optional[str] = None,
                 database_name: Optional[str] = None,
                 headers_collection: str = "invoice_headers",
                 items_collection: str = "invoice_items") -> None:
        self.conn_str = connection_string or settings.MONGODB_URL
        self.db_name = database_name or settings.MONGODB_DATABASE
        self.headers_collection_name = headers_collection
        self.items_collection_name = items_collection
        self._client: Optional[MongoClient] = None

    def _get_db(self):
        if not self._client:
            self._client = MongoClient(self.conn_str, serverSelectionTimeoutMS=5000)
            self._client.admin.command('ping')
            logger.info("✅ Conectado a MongoDB (repo)")
        return self._client[self.db_name]

    def _headers(self) -> Collection:
        coll = self._get_db()[self.headers_collection_name]
        try:
            coll.create_index("_id", unique=True)
            coll.create_index([("emisor.ruc", 1), ("fecha_emision", -1)])
            coll.create_index("mes_proceso")
            coll.create_index("owner_email")
        except Exception:
            pass
        return coll

    def _items(self) -> Collection:
        coll = self._get_db()[self.items_collection_name]
        try:
            coll.create_index([("header_id", 1), ("linea", 1)], unique=True)
            coll.create_index("owner_email")
        except Exception:
            pass
        return coll

    def upsert_header(self, header: InvoiceHeader) -> None:
        doc = header.model_dump()
        doc["updated_at"] = datetime.utcnow()
        self._headers().replace_one({"_id": header.id}, doc, upsert=True)

    def replace_items(self, header_id: str, items: List[InvoiceDetail]) -> int:
        items_coll = self._items()
        items_coll.delete_many({"header_id": header_id})
        if not items:
            return 0
        to_insert = [it.model_dump() for it in items]
        res = items_coll.insert_many(to_insert)
        return len(res.inserted_ids)

    def close(self):
        try:
            if self._client:
                self._client.close()
        except Exception:
            pass
