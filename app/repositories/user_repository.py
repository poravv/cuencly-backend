from __future__ import annotations
from typing import Optional, Dict, Any
from datetime import datetime
from pymongo import MongoClient
from pymongo.collection import Collection
from app.config.settings import settings


class UserRepository:
    def __init__(self, conn_str: Optional[str] = None, db_name: Optional[str] = None, collection: str = "auth_users"):
        self.conn_str = conn_str or settings.MONGODB_URL
        self.db_name = db_name or settings.MONGODB_DATABASE
        self.collection = collection
        self._client: Optional[MongoClient] = None

    def _coll(self) -> Collection:
        if not self._client:
            self._client = MongoClient(self.conn_str, serverSelectionTimeoutMS=5000)
            self._client.admin.command('ping')
        db = self._client[self.db_name]
        coll = db[self.collection]
        try:
            coll.create_index('email', unique=True)
            coll.create_index('uid')
        except Exception:
            pass
        return coll

    def upsert_user(self, user: Dict[str, Any]) -> None:
        now = datetime.utcnow()
        email = (user.get('email') or '').lower()
        payload = {
            'email': email,
            'uid': user.get('uid') or user.get('user_id'),
            'name': user.get('name') or user.get('displayName'),
            'picture': user.get('picture') or user.get('photoURL'),
            'last_login': now,
        }
        self._coll().update_one({'email': email}, {'$setOnInsert': {'created_at': now}, '$set': payload}, upsert=True)

    def get_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        return self._coll().find_one({'email': email.lower()})

