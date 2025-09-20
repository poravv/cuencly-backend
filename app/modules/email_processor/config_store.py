import logging
import uuid
from typing import List, Dict, Any, Optional

from pymongo import MongoClient
from pymongo.collection import Collection

from app.config.settings import settings

logger = logging.getLogger(__name__)

COLLECTION_NAME = "email_configs"


def _get_client() -> MongoClient:
    mongo_url = getattr(settings, "MONGODB_URL", None) or "mongodb://localhost:27017/"
    client = MongoClient(
        mongo_url,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=10000,
        socketTimeoutMS=20000,
        maxPoolSize=20,
        minPoolSize=1,
    )
    # smoke test
    client.admin.command("ping")
    return client


def _get_collection() -> Collection:
    client = _get_client()
    db_name = getattr(settings, "MONGODB_DATABASE", "cuenlyapp_warehouse")
    db = client[db_name]
    coll = db[COLLECTION_NAME]
    try:
        coll.create_index("username")
        coll.create_index([("enabled", 1)])
        coll.create_index([("provider", 1)])
    except Exception:
        pass
    return coll


def list_configs(include_password: bool = False, owner_email: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all email configurations. Password is omitted by default."""
    coll = _get_collection()
    query: Dict[str, Any] = {}
    if owner_email:
        query['owner_email'] = owner_email.lower()
    docs = list(coll.find(query))
    results: List[Dict[str, Any]] = []
    for d in docs:
        item = {
            "id": str(d.get("_id")),
            "name": d.get("name") or "",
            "host": d.get("host") or "",
            "port": int(d.get("port") or 993),
            "username": d.get("username") or "",
            "use_ssl": bool(d.get("use_ssl", True)),
            "search_criteria": d.get("search_criteria") or "UNSEEN",
            "search_terms": d.get("search_terms") or [],
            "provider": d.get("provider") or "other",
            "enabled": bool(d.get("enabled", True)),
        }
        if include_password:
            item["password"] = d.get("password") or ""
        results.append(item)
    return results


def get_enabled_configs(include_password: bool = True, owner_email: Optional[str] = None) -> List[Dict[str, Any]]:
    coll = _get_collection()
    q: Dict[str, Any] = {"enabled": True}
    if owner_email:
        q['owner_email'] = owner_email.lower()
    docs = list(coll.find(q))
    configs = []
    for d in docs:
        cfg = {
            "id": str(d.get("_id")),
            "name": d.get("name") or "",
            "host": d.get("host") or "",
            "port": int(d.get("port") or 993),
            "username": d.get("username") or "",
            "use_ssl": bool(d.get("use_ssl", True)),
            "search_criteria": d.get("search_criteria") or "UNSEEN",
            "search_terms": d.get("search_terms") or [],
            "provider": d.get("provider") or "other",
            "enabled": True,
        }
        if include_password:
            cfg["password"] = d.get("password") or ""
        configs.append(cfg)
    return configs


def create_config(data: Dict[str, Any], owner_email: Optional[str] = None) -> str:
    coll = _get_collection()
    payload = {
        "_id": data.get("id") or uuid.uuid4().hex,
        "name": data.get("name") or data.get("username") or "",
        "host": data.get("host") or "",
        "port": int(data.get("port") or 993),
        "username": data.get("username") or "",
        "password": data.get("password") or "",
        "use_ssl": bool(data.get("use_ssl", True)),
        "search_criteria": data.get("search_criteria") or "UNSEEN",
        "search_terms": data.get("search_terms") or [],
        "provider": data.get("provider") or "other",
        "enabled": bool(data.get("enabled", True)),
        "owner_email": (owner_email or data.get("owner_email") or "").lower(),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
    }
    coll.insert_one(payload)
    return str(payload["_id"]) 


def update_config(config_id: str, data: Dict[str, Any], owner_email: Optional[str] = None) -> bool:
    coll = _get_collection()
    updates = {}
    for key in [
        "name",
        "host",
        "port",
        "username",
        "password",
        "use_ssl",
        "search_criteria",
        "search_terms",
        "provider",
        "enabled",
        "updated_at",
    ]:
        if key in data:
            updates[key] = data[key]
    if not updates:
        return False
    q = {"_id": config_id}
    if owner_email:
        q['owner_email'] = owner_email.lower()
    res = coll.update_one(q, {"$set": updates})
    return res.matched_count > 0


def delete_config(config_id: str, owner_email: Optional[str] = None) -> bool:
    coll = _get_collection()
    q = {"_id": config_id}
    if owner_email:
        q['owner_email'] = owner_email.lower()
    res = coll.delete_one(q)
    return res.deleted_count > 0


def set_enabled(config_id: str, enabled: bool, owner_email: Optional[str] = None) -> bool:
    coll = _get_collection()
    q = {"_id": config_id}
    if owner_email:
        q['owner_email'] = owner_email.lower()
    res = coll.update_one(q, {"$set": {"enabled": bool(enabled)}})
    return res.matched_count > 0


def toggle_enabled(config_id: str, owner_email: Optional[str] = None) -> Optional[bool]:
    coll = _get_collection()
    q = {"_id": config_id}
    if owner_email:
        q['owner_email'] = owner_email.lower()
    doc = coll.find_one(q, {"enabled": 1})
    if not doc:
        return None
    new_val = not bool(doc.get("enabled", True))
    coll.update_one(q, {"$set": {"enabled": new_val}})
    return new_val


def get_by_id(config_id: str, include_password: bool = True, owner_email: Optional[str] = None) -> Optional[Dict[str, Any]]:
    coll = _get_collection()
    projection = None if include_password else {"password": 0}
    q = {"_id": config_id}
    if owner_email:
        q['owner_email'] = owner_email.lower()
    d = coll.find_one(q, projection)
    if not d:
        return None
    return {
        "id": str(d.get("_id")),
        "name": d.get("name"),
        "host": d.get("host"),
        "port": int(d.get("port", 993)),
        "username": d.get("username"),
        "password": d.get("password") if include_password else None,
        "use_ssl": bool(d.get("use_ssl", True)),
        "search_criteria": d.get("search_criteria") or "UNSEEN",
        "search_terms": d.get("search_terms") or [],
        "provider": d.get("provider") or "other",
        "enabled": bool(d.get("enabled", True)),
    }


def get_by_username(username: str, include_password: bool = True, owner_email: Optional[str] = None) -> Optional[Dict[str, Any]]:
    coll = _get_collection()
    projection = None if include_password else {"password": 0}
    q = {"username": username}
    if owner_email:
        q['owner_email'] = owner_email.lower()
    d = coll.find_one(q, projection)
    if not d:
        return None
    return {
        "id": str(d.get("_id")),
        "name": d.get("name"),
        "host": d.get("host"),
        "port": int(d.get("port", 993)),
        "username": d.get("username"),
        "password": d.get("password") if include_password else None,
        "use_ssl": bool(d.get("use_ssl", True)),
        "search_criteria": d.get("search_criteria") or "UNSEEN",
        "search_terms": d.get("search_terms") or [],
        "provider": d.get("provider") or "other",
        "enabled": bool(d.get("enabled", True)),
    }
