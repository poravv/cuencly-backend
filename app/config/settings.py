import os
import json
from typing import List, Dict, Any
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

# Cargar variables de entorno desde el archivo .env
#load_dotenv()
load_dotenv(encoding="utf-8")

class Settings(BaseSettings):
    # App
    TEMP_PDF_DIR: str = os.getenv("TEMP_PDF_DIR", "./data/temp_pdfs")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # OpenAI
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

    # MongoDB
    MONGODB_URL: str = os.getenv("MONGODB_URL", "mongodb://cuenlyapp:cuenlyapp2025@mongodb:27017/cuenlyapp_warehouse?authSource=admin")
    MONGODB_DATABASE: str = os.getenv("MONGODB_DATABASE", "cuenlyapp_warehouse")
    MONGODB_COLLECTION: str = os.getenv("MONGODB_COLLECTION", "facturas_completas")

    # API
    API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
    API_PORT: int = int(os.getenv("API_PORT", 8000))

    # Job
    JOB_INTERVAL_MINUTES: int = int(os.getenv("JOB_INTERVAL_MINUTES", 60))

    # Auth / Multi-tenant
    AUTH_REQUIRE: bool = os.getenv("AUTH_REQUIRE", "true").lower() in ("1", "true", "yes")
    FIREBASE_PROJECT_ID: str = os.getenv("FIREBASE_PROJECT_ID", "cuenly-app")
    MULTI_TENANT_ENFORCE: bool = os.getenv("MULTI_TENANT_ENFORCE", "true").lower() in ("1", "true", "yes")
    
    model_config = {
        "env_file": ".env",
        "extra": "ignore"  # Ignorar campos adicionales en lugar de lanzar un error
    }

    def model_post_init(self, __context):
        # No post-init dynamic env parsing needed; configs come from MongoDB
        pass

settings = Settings()
