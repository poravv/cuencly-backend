import os
import json
from typing import List, Dict, Any
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

# Cargar variables de entorno desde el archivo .env
#load_dotenv()
load_dotenv(encoding="utf-8")

class Settings(BaseSettings):
    # Configuraciones de Email (deprecadas: se gestionan vía MongoDB)
    EMAIL_HOST: str = os.getenv("EMAIL_HOST", "")
    EMAIL_PORT: int = int(os.getenv("EMAIL_PORT", 993))
    EMAIL_USERNAME: str = os.getenv("EMAIL_USERNAME", "")
    EMAIL_PASSWORD: str = os.getenv("EMAIL_PASSWORD", "")
    EMAIL_USE_SSL: bool = os.getenv("EMAIL_USE_SSL", "True").lower() == "true"

    # Configuraciones de post-procesamiento
    POSTPROCESS_ENABLE_RECALC: bool = (os.getenv("POSTPROCESS_ENABLE_RECALC", "true").lower() == "true")
    POSTPROCESS_ENABLE_RECONCILE: bool = (os.getenv("POSTPROCESS_ENABLE_RECONCILE", "true").lower() == "true")
    POSTPROCESS_RECONCILE_TOLERANCE: int = int(os.getenv("POSTPROCESS_RECONCILE_TOLERANCE", 2))
    
    # Configuraciones para múltiples correos (legacy, sin uso)
    EMAILS_CONFIG: List[Dict[str, Any]] = []
    
    # Configuraciones de la App
    EXCEL_OUTPUT_PATH: str = os.getenv("EXCEL_OUTPUT_PATH", "/app/data/facturas.xlsx")
    EXCEL_OUTPUT_DIR: str = os.getenv("EXCEL_OUTPUT_DIR", "/app/data/excels")  # Directorio para archivos por mes
    TEMP_PDF_DIR: str = os.getenv("TEMP_PDF_DIR", "./data/temp_pdfs")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    
    # Configuración de OpenAI
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    
    # Configuración de MongoDB
    MONGODB_URL: str = os.getenv("MONGODB_URL", "mongodb://invoicesync:invoicesync2025@mongodb:27017/invoicesync_warehouse?authSource=admin")
    MONGODB_DATABASE: str = os.getenv("MONGODB_DATABASE", "invoicesync_warehouse")
    MONGODB_COLLECTION: str = os.getenv("MONGODB_COLLECTION", "facturas_completas")
    
    # Configuraciones de la API
    API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
    API_PORT: int = int(os.getenv("API_PORT", 8000))
    
    # Configuraciones del Job
    JOB_INTERVAL_MINUTES: int = int(os.getenv("JOB_INTERVAL_MINUTES", 60))
    EMAIL_SEARCH_CRITERIA: str = os.getenv("EMAIL_SEARCH_CRITERIA", "UNSEEN")
    EMAIL_SEARCH_TERMS: List[str] = []
    
    model_config = {
        "env_file": ".env",
        "extra": "ignore"  # Ignorar campos adicionales en lugar de lanzar un error
    }

    def model_post_init(self, __context):
        # Procesamiento manual para EMAIL_SEARCH_TERMS
        search_terms_str = os.getenv("EMAIL_SEARCH_TERMS", '["factura","facturacion","factura electronica","comprobante","documento electrónico","documento electronico"]')
        try:
            self.EMAIL_SEARCH_TERMS = json.loads(search_terms_str)
        except json.JSONDecodeError:
            # Fallback para el formato antiguo
            self.EMAIL_SEARCH_TERMS = [term.strip() for term in search_terms_str.split(",")]
        
        # Deshabilitar carga de EMAILS_CONFIG desde .env (se usa MongoDB)
        self.EMAILS_CONFIG = []
    
    def get_gmail_configs(self) -> List[Dict[str, Any]]:
        """Deprecated: configs are stored in MongoDB now."""
        return []
    
    def get_all_email_configs(self) -> List[Dict[str, Any]]:
        """Deprecated: use MongoDB config store via API."""
        return []

settings = Settings()
