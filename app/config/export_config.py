"""
Configuración para los nuevos exportadores - MongoDB como almacenamiento primario
"""
import os
from typing import Optional

# Configuración MongoDB - PRIMARIO
MONGODB_CONNECTION_STRING = os.getenv("MONGODB_URL", "mongodb://cuenlyapp:cuenlyapp2025@mongodb:27017/cuenlyapp_warehouse?authSource=admin")
MONGODB_DATABASE_NAME = os.getenv("MONGODB_DATABASE", "cuenlyapp_warehouse")
MONGODB_COLLECTION_NAME = os.getenv("MONGODB_COLLECTION", "facturas_completas")

# MongoDB como almacenamiento primario
MONGODB_AS_PRIMARY = os.getenv("MONGODB_AS_PRIMARY", "true").lower() == "true"
AUTO_EXPORT_TO_MONGODB = os.getenv("AUTO_EXPORT_TO_MONGODB", "true").lower() == "true"

# Configuración de funcionalidades (solo MongoDB)
ENABLE_MONGODB_EXPORT = os.getenv("ENABLE_MONGODB_EXPORT", "true").lower() == "true"

# Configuración de performance
MONGO_BULK_SIZE = int(os.getenv("MONGO_BULK_SIZE", "100"))

# Configuración de retención
MONGO_DATA_RETENTION_DAYS = int(os.getenv("MONGO_DATA_RETENTION_DAYS", "365"))

def get_mongodb_config() -> dict:
    """Obtiene configuración completa de MongoDB"""
    return {
        "connection_string": MONGODB_CONNECTION_STRING,
        "database_name": MONGODB_DATABASE_NAME,
        "collection_name": MONGODB_COLLECTION_NAME,
        "bulk_size": MONGO_BULK_SIZE,
        "retention_days": MONGO_DATA_RETENTION_DAYS,
        "enabled": ENABLE_MONGODB_EXPORT,
        "as_primary": MONGODB_AS_PRIMARY,
        "auto_export": AUTO_EXPORT_TO_MONGODB
    }
