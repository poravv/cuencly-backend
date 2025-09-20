from __future__ import annotations
from abc import ABC, abstractmethod
from typing import List

from app.models.invoice_v2 import InvoiceHeader, InvoiceDetail, InvoiceDocument


class InvoiceRepository(ABC):
    @abstractmethod
    def upsert_header(self, header: InvoiceHeader) -> None:
        ...

    @abstractmethod
    def replace_items(self, header_id: str, items: List[InvoiceDetail]) -> int:
        ...

    def save_document(self, doc: InvoiceDocument) -> None:
        self.upsert_header(doc.header)
        self.replace_items(doc.header.id, doc.items)

    def save_many(self, docs: List[InvoiceDocument]) -> None:
        for d in docs:
            self.save_document(d)

