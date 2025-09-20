from fastapi import FastAPI, BackgroundTasks, Depends, HTTPException, UploadFile, File, Form, Query, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import logging
import uvicorn
import uuid
import time
from typing import List, Optional, Dict, Any
import shutil
from datetime import datetime

# Importar validadores de seguridad
from app.utils.validators import SecurityValidators, DataValidators, ValidationError, log_security_event
from fastapi.responses import FileResponse
from fastapi import Response
from pydantic import BaseModel

from app.config.settings import settings
from app.utils.firebase_auth import verify_firebase_token, extract_bearer_token
from app.repositories.user_repository import UserRepository
from app.models.models import InvoiceData, EmailConfig, ProcessResult, JobStatus, MultiEmailConfig
from app.main import CuenlyApp
from app.modules.scheduler.processing_lock import PROCESSING_LOCK
from app.modules.scheduler.task_queue import task_queue
from app.modules.email_processor.storage import save_binary
from app.modules.prefs.prefs import get_auto_refresh as prefs_get_auto_refresh, set_auto_refresh as prefs_set_auto_refresh
from app.modules.mongo_exporter import MongoDBExporter
from app.repositories.mongo_invoice_repository import MongoInvoiceRepository
from app.modules.mapping.invoice_mapping import map_invoice
from app.modules.mongo_query_service import get_mongo_query_service
from app.repositories.mongo_invoice_repository import MongoInvoiceRepository
from app.modules.email_processor.config_store import (
    list_configs as db_list_configs,
    create_config as db_create_config,
    update_config as db_update_config,
    delete_config as db_delete_config,
    set_enabled as db_set_enabled,
    toggle_enabled as db_toggle_enabled,
    get_by_id as db_get_by_id,
    get_by_username as db_get_by_username,
)

# Configurar logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("cuenlyapp_api.log")
    ]
)

logger = logging.getLogger(__name__)

# Crear la aplicación FastAPI
app = FastAPI(
    title="CuenlyApp API",
    description="API para procesar facturas desde correo electrónico y almacenarlas en MongoDB",
    version="2.0.0"
)

# Configurar CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En producción, limitar a dominios específicos
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Instancia global del procesador
invoice_sync = CuenlyApp()

# Payloads
class IntervalPayload(BaseModel):
    minutes: int

# Preferencias UI
class AutoRefreshPayload(BaseModel):
    enabled: bool
    interval_ms: int = 30000
    uid: Optional[str] = None

class AutoRefreshPref(BaseModel):
    uid: str
    enabled: bool
    interval_ms: int

class ToggleEnabledPayload(BaseModel):
    enabled: bool

def _get_current_user(request: Request) -> Dict[str, Any]:
    """Valida token Firebase y retorna claims. Upserta usuario en DB."""
    token = extract_bearer_token(request)
    if not token:
        if settings.AUTH_REQUIRE:
            raise HTTPException(status_code=401, detail="Authorization requerido")
        return {}
    claims = verify_firebase_token(token)
    try:
        UserRepository().upsert_user({
            'email': claims.get('email'),
            'uid': claims.get('user_id'),
            'name': claims.get('name'),
            'picture': claims.get('picture'),
        })
    except Exception:
        pass
    return claims

# Tarea en segundo plano para procesar correos
def process_emails_task():
    """Tarea en segundo plano para procesar correos."""
    try:
        result = invoice_sync.process_emails()
        logger.info(f"Tarea en segundo plano completada: {result.message}")
    except Exception as e:
        logger.error(f"Error en tarea en segundo plano: {str(e)}")

@app.get("/")
async def root():
    """Endpoint raíz para verificar que la API está funcionando."""
    return {"message": "CuenlyApp API está en funcionamiento"}

@app.post("/process", response_model=ProcessResult)
async def process_emails(background_tasks: BackgroundTasks, run_async: bool = False, request: Request = None, user: Dict[str, Any] = Depends(_get_current_user)):
    """
    Procesa correos electrónicos para extraer facturas.
    
    Args:
        background_tasks: Gestor de tareas en segundo plano.
        run_async: Si es True, el procesamiento se ejecuta en segundo plano.
        
    Returns:
        ProcessResult: Resultado del procesamiento.
    """
    try:
        if run_async:
            # Ejecutar en segundo plano
            background_tasks.add_task(process_emails_task)
            return ProcessResult(
                success=True,
                message="Procesamiento iniciado en segundo plano"
            )
        else:
            # Ejecutar de forma síncrona
            # Procesar solo cuentas del usuario (multiusuario)
            from app.modules.email_processor.config_store import get_enabled_configs
            from app.modules.email_processor.email_processor import MultiEmailProcessor
            owner_email = (user.get('email') or '').lower()
            configs = get_enabled_configs(include_password=True, owner_email=owner_email) if owner_email else []
            if not configs:
                return ProcessResult(success=False, message="Sin cuentas de correo habilitadas para este usuario", invoice_count=0)
            mp = MultiEmailProcessor(email_configs=[MultiEmailConfig(**c) for c in configs], owner_email=owner_email)
            result = mp.process_all_emails()
            return result
    except Exception as e:
        logger.error(f"Error al procesar correos: {str(e)}")
        return ProcessResult(
            success=False,
            message=f"Error al procesar correos: {str(e)}"
        )

@app.post("/process-direct")
async def process_emails_direct(user: Dict[str, Any] = Depends(_get_current_user)):
    """Procesa correos directamente sin cola de tareas (modo simple)."""
    try:
        # Ejecutar procesamiento directamente
        from app.modules.email_processor.config_store import get_enabled_configs
        from app.modules.email_processor.email_processor import MultiEmailProcessor
        owner_email = (user.get('email') or '').lower()
        configs = get_enabled_configs(include_password=True, owner_email=owner_email) if owner_email else []
        if not configs:
            return {"success": False, "message": "Sin cuentas de correo habilitadas para este usuario", "invoice_count": 0}
        mp = MultiEmailProcessor(email_configs=[MultiEmailConfig(**c) for c in configs], owner_email=owner_email)
        result = mp.process_all_emails()
        
        if result and hasattr(result, 'success') and result.success:
            return {
                "success": True,
                "message": result.message,
                "invoice_count": getattr(result, 'invoice_count', 0)
            }
        else:
            return {
                "success": False,
                "message": getattr(result, 'message', 'Error en el procesamiento'),
                "invoice_count": 0
            }
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

@app.post("/tasks/process")
async def enqueue_process_emails(user: Dict[str, Any] = Depends(_get_current_user)):
    """Encola una ejecución de procesamiento de correos y retorna un job_id."""
    
    # Verificar si el job automático está ejecutándose
    job_status = invoice_sync.get_job_status()
    if job_status.running:
        # Retornar error inmediatamente si el job automático está activo
        job_id = str(uuid.uuid4().hex)
        task_queue._jobs[job_id] = {
            'job_id': job_id,
            'action': 'process_emails',
            'status': 'error',
            'created_at': time.time(),
            'started_at': time.time(),
            'finished_at': time.time(),
            'message': 'No se puede procesar manualmente mientras la automatización esté activa. Detenga la automatización primero.',
            'result': ProcessResult(
                success=False,
                message='No se puede procesar manualmente mientras la automatización esté activa. Detenga la automatización primero.',
                invoice_count=0,
                processed_emails=0
            ),
            '_func': None,
        }
        return {"job_id": job_id}
    
    def _runner():
        from app.modules.email_processor.config_store import get_enabled_configs
        from app.modules.email_processor.email_processor import MultiEmailProcessor
        owner_email = (user.get('email') or '').lower()
        configs = get_enabled_configs(include_password=True, owner_email=owner_email) if owner_email else []
        mp = MultiEmailProcessor(email_configs=[MultiEmailConfig(**c) for c in configs], owner_email=owner_email)
        return mp.process_all_emails()

    job_id = task_queue.enqueue("process_emails", _runner)
    return {"job_id": job_id}

@app.get("/tasks/{job_id}")
async def get_task_status(job_id: str):
    """Consulta el estado de un job enviado a la cola."""
    job = task_queue.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job no encontrado")
    return job

@app.delete("/tasks/cleanup")
async def cleanup_old_tasks():
    """Limpia tareas antiguas que están atoradas."""
    cleanup_count = 0
    current_time = time.time()
    
    # Limpiar tareas que llevan más de 1 hora atoradas
    with task_queue._lock:
        jobs_to_remove = []
        for job_id, job in task_queue._jobs.items():
            if job.get('status') == 'running':
                created_at = job.get('created_at', current_time)
                # Si la tarea lleva más de 1 hora "running", marcarla como error
                if current_time - created_at > 3600:  # 1 hora
                    job['status'] = 'error'
                    job['message'] = 'Tarea cancelada por tiempo excesivo'
                    job['finished_at'] = current_time
                    cleanup_count += 1
                    
            # Eliminar tareas completadas que tengan más de 24 horas
            elif job.get('status') in ['done', 'error']:
                created_at = job.get('created_at', current_time)
                if current_time - created_at > 86400:  # 24 horas
                    jobs_to_remove.append(job_id)
                    cleanup_count += 1
        
        # Remover tareas antiguas
        for job_id in jobs_to_remove:
            del task_queue._jobs[job_id]
    
    return {"message": f"Se limpiaron {cleanup_count} tareas", "cleaned_count": cleanup_count}

@app.get("/tasks/debug")
async def debug_tasks(user: Dict[str, Any] = Depends(_get_current_user)):
    """Debug endpoint para ver el estado de todas las tareas."""
    current_time = time.time()
    task_info = []
    
    with task_queue._lock:
        for job_id, job in task_queue._jobs.items():
            job_copy = {k: v for k, v in job.items() if k != '_func'}
            created_at = job.get('created_at', current_time)
            running_time = current_time - created_at
            job_copy['running_time_seconds'] = running_time
            task_info.append(job_copy)
    
    return {
        "total_tasks": len(task_info),
        "tasks": task_info,
        "processing_lock_available": not PROCESSING_LOCK.locked()
    }

@app.post("/upload", response_model=ProcessResult)
async def upload_pdf(
    file: UploadFile = File(...),
    sender: Optional[str] = Form(None),
    date: Optional[str] = Form(None)
    , user: Dict[str, Any] = Depends(_get_current_user)):
    """
    Sube un archivo PDF para procesarlo directamente.
    
    Args:
        file: Archivo PDF a procesar.
        sender: Remitente (opcional).
        date: Fecha del documento (opcional).
        
    Returns:
        ProcessResult: Resultado del procesamiento.
    """
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos PDF")
    
    try:
        # Guardar el archivo
        pdf_path = os.path.join(settings.TEMP_PDF_DIR, file.filename)
        with open(pdf_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # Preparar metadatos
        email_meta = {
            "sender": sender or "Carga manual",
        }

        # Convertir fecha si se proporciona
        if date:
            try:
                email_meta["date"] = datetime.strptime(date, "%Y-%m-%d")
            except Exception:
                logger.warning(f"Formato de fecha incorrecto: {date}")

        # Extraer + guardar en esquema v2 (invoice_headers/items)
        with PROCESSING_LOCK:
            invoice_data = invoice_sync.openai_processor.extract_invoice_data(pdf_path, email_meta)
            invoices = [invoice_data] if invoice_data else []
            if invoices:
                try:
                    repo = MongoInvoiceRepository()
                    owner = (user.get('email') or '').lower()
                    doc = map_invoice(invoice_data, fuente="OPENAI_VISION")
                    if owner:
                        try:
                            doc.header.owner_email = owner
                            for it in doc.items:
                                it.owner_email = owner
                        except Exception:
                            pass
                    repo.save_document(doc)
                except Exception as e:
                    logger.error(f"❌ Error persistiendo v2 (upload PDF): {e}")

        if not invoices:
            return ProcessResult(
                success=False,
                message="No se pudo extraer factura del PDF",
                invoice_count=0,
                invoices=[]
            )

        return ProcessResult(
            success=True,
            message=f"Factura procesada y almacenada (v2)",
            invoice_count=1,
            invoices=invoices
        )
        
    except Exception as e:
        logger.error(f"Error al procesar el archivo: {str(e)}")
        return ProcessResult(
            success=False,
            message=f"Error al procesar el archivo: {str(e)}"
        )

@app.post("/upload-xml", response_model=ProcessResult)
async def upload_xml(
    file: UploadFile = File(...),
    sender: Optional[str] = Form(None),
    date: Optional[str] = Form(None)
    , user: Dict[str, Any] = Depends(_get_current_user)):
    """
    Sube un archivo XML SIFEN para procesarlo directamente con el parser nativo (fallback OpenAI).
    """
    if not (file.filename.lower().endswith('.xml')):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos XML")

    try:
        # Guardar el archivo XML
        xml_path = os.path.join(settings.TEMP_PDF_DIR, file.filename)
        with open(xml_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # Metadatos opcionales
        email_meta = {
            "sender": sender or "Carga manual",
        }
        if date:
            try:
                email_meta["date"] = datetime.strptime(date, "%Y-%m-%d")
            except Exception:
                logger.warning(f"Formato de fecha incorrecto: {date}")

        with PROCESSING_LOCK:
            # Procesar XML y almacenar en esquema v2
            invoice_data = invoice_sync.openai_processor.extract_invoice_data_from_xml(xml_path, email_meta)
            invoices = [invoice_data] if invoice_data else []
            if invoices:
                try:
                    repo = MongoInvoiceRepository()
                    owner = (user.get('email') or '').lower()
                    doc = map_invoice(invoice_data, fuente="XML_NATIVO" if getattr(invoice_data, 'cdc', '') else "OPENAI_VISION")
                    if owner:
                        try:
                            doc.header.owner_email = owner
                            for it in doc.items:
                                it.owner_email = owner
                        except Exception:
                            pass
                    repo.save_document(doc)
                except Exception as e:
                    logger.error(f"❌ Error persistiendo v2 (upload XML): {e}")

        if not invoices:
            return ProcessResult(
                success=False,
                message="No se pudo extraer información desde el XML",
                invoice_count=0,
                invoices=[]
            )

        return ProcessResult(
            success=True,
            message=f"Factura XML procesada y almacenada (v2)",
            invoice_count=1,
            invoices=invoices
        )

    except Exception as e:
        logger.error(f"Error al procesar el XML: {str(e)}")
        return ProcessResult(
            success=False,
            message=f"Error al procesar el XML: {str(e)}"
        )

@app.post("/tasks/upload-pdf")
async def enqueue_upload_pdf(
    file: UploadFile = File(...),
    sender: Optional[str] = Form(None),
    date: Optional[str] = Form(None)
    , user: Dict[str, Any] = Depends(_get_current_user)):
    """Encola el procesamiento de un PDF manual y retorna job_id."""
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos PDF")

    try:
        file_bytes = await file.read()
        pdf_path = save_binary(file_bytes, file.filename, force_pdf=True)
        email_meta = {"sender": sender or "Carga manual"}
        if date:
            try:
                email_meta["date"] = datetime.strptime(date, "%Y-%m-%d")
            except Exception:
                logger.warning(f"Formato de fecha incorrecto: {date}")

        def _runner():
            inv = invoice_sync.openai_processor.extract_invoice_data(pdf_path, email_meta)
            invoices = [inv] if inv else []
            if invoices:
                try:
                    repo = MongoInvoiceRepository()
                    owner = (user.get('email') or '').lower()
                    doc = map_invoice(inv, fuente="OPENAI_VISION")
                    if owner:
                        try:
                            doc.header.owner_email = owner
                            for it in doc.items:
                                it.owner_email = owner
                        except Exception:
                            pass
                    repo.save_document(doc)
                except Exception as e:
                    logger.error(f"❌ Error persistiendo v2 (tasks upload PDF): {e}")
            return ProcessResult(
                success=bool(invoices),
                message=("Factura procesada y almacenada (v2)" if invoices else "No se pudo extraer factura"),
                invoice_count=len(invoices),
                invoices=invoices
            )

        job_id = task_queue.enqueue("upload_pdf", _runner)
        return {"job_id": job_id}
    except Exception as e:
        logger.error(f"Error al encolar PDF: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/tasks/upload-xml")
async def enqueue_upload_xml(
    file: UploadFile = File(...),
    sender: Optional[str] = Form(None),
    date: Optional[str] = Form(None)
    , user: Dict[str, Any] = Depends(_get_current_user)):
    """Encola el procesamiento de un XML manual y retorna job_id."""
    if not file.filename.lower().endswith('.xml'):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos XML")

    try:
        file_bytes = await file.read()
        xml_path = save_binary(file_bytes, file.filename)
        email_meta = {"sender": sender or "Carga manual"}
        if date:
            try:
                email_meta["date"] = datetime.strptime(date, "%Y-%m-%d")
            except Exception:
                logger.warning(f"Formato de fecha incorrecto: {date}")

        def _runner():
            inv = invoice_sync.openai_processor.extract_invoice_data_from_xml(xml_path, email_meta)
            invoices = [inv] if inv else []
            if invoices:
                try:
                    repo = MongoInvoiceRepository()
                    owner = (user.get('email') or '').lower()
                    doc = map_invoice(inv, fuente="XML_NATIVO" if getattr(inv, 'cdc', '') else "OPENAI_VISION")
                    if owner:
                        try:
                            doc.header.owner_email = owner
                            for it in doc.items:
                                it.owner_email = owner
                        except Exception:
                            pass
                    repo.save_document(doc)
                except Exception as e:
                    logger.error(f"❌ Error persistiendo v2 (tasks upload XML): {e}")
            return ProcessResult(
                success=bool(invoices),
                message=("Factura XML procesada y almacenada (v2)" if invoices else "No se pudo extraer información desde el XML"),
                invoice_count=len(invoices),
                invoices=invoices
            )

        job_id = task_queue.enqueue("upload_xml", _runner)
        return {"job_id": job_id}
    except Exception as e:
        logger.error(f"Error al encolar XML: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/excel")
async def get_excel(user: Dict[str, Any] = Depends(_get_current_user)):
    raise HTTPException(status_code=410, detail="Exportación a Excel deshabilitada")

@app.get("/excel/list")
async def list_excel_files(user: Dict[str, Any] = Depends(_get_current_user)):
    raise HTTPException(status_code=410, detail="Exportación a Excel deshabilitada")

@app.get("/excel/{year_month}")
async def get_excel_by_month(year_month: str, user: Dict[str, Any] = Depends(_get_current_user)):
    raise HTTPException(status_code=410, detail="Exportación a Excel deshabilitada")

@app.post("/email-config/test")
async def test_email_config(config: MultiEmailConfig, user: Dict[str, Any] = Depends(_get_current_user)):
    """
    Prueba la conexión a una configuración de correo.
    
    Args:
        config: Configuración de correo a probar
        
    Returns:
        dict: Resultado de la prueba
    """
    try:
        from app.modules.email_processor.email_processor import EmailProcessor
        from app.models.models import EmailConfig
        
        # Resolver password ausente con DB por id o username
        pwd = config.password
        if not pwd:
            db_cfg = None
            owner_email = (user.get('email') or '').lower()
            if config.id:
                db_cfg = db_get_by_id(config.id, include_password=True, owner_email=owner_email)
            if not db_cfg and config.username:
                db_cfg = db_get_by_username(config.username, include_password=True, owner_email=owner_email)
            if db_cfg and db_cfg.get("password"):
                pwd = db_cfg.get("password")

        # Crear configuración temporal para probar
        test_config = EmailConfig(
            host=config.host,
            port=config.port,
            username=config.username,
            password=pwd or "",
            search_criteria=config.search_criteria,
            search_terms=config.search_terms or []
        )
        
        # Crear procesador temporal
        processor = EmailProcessor(test_config)
        
        # Intentar conectar
        success = processor.connect()
        processor.disconnect()
        
        if success:
            return {"success": True, "message": "Conexión exitosa"}
        else:
            return {"success": False, "message": "Error al conectar"}
            
    except Exception as e:
        logger.error(f"Error al probar configuración de correo: {str(e)}")
        return {"success": False, "message": f"Error: {str(e)}"}

@app.post("/email-configs/{config_id}/test")
async def test_email_config_by_id(config_id: str, user: Dict[str, Any] = Depends(_get_current_user)):
    """Prueba una configuración guardada, identificada por su ID en MongoDB."""
    try:
        from app.modules.email_processor.email_processor import EmailProcessor
        from app.models.models import EmailConfig

        db_cfg = db_get_by_id(config_id, include_password=True, owner_email=(user.get('email') or '').lower())
        if not db_cfg:
            raise HTTPException(status_code=404, detail="Configuración no encontrada")

        test_config = EmailConfig(
            host=db_cfg.get("host"),
            port=int(db_cfg.get("port", 993)),
            username=db_cfg.get("username"),
            password=db_cfg.get("password") or "",
            search_criteria=db_cfg.get("search_criteria") or "UNSEEN",
            search_terms=db_cfg.get("search_terms") or []
        )

        processor = EmailProcessor(test_config)
        success = processor.connect()
        processor.disconnect()

        if success:
            return {"success": True, "message": "Conexión exitosa"}
        else:
            return {"success": False, "message": "Error al conectar"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error al probar configuración por ID: {e}")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


# -----------------------------
# Email Config CRUD (MongoDB)
# -----------------------------

@app.get("/email-configs")
async def list_email_configs(user: Dict[str, Any] = Depends(_get_current_user)):
    try:
        cfgs = db_list_configs(include_password=False, owner_email=(user.get('email') or '').lower())
        return {"success": True, "configs": cfgs, "total": len(cfgs)}
    except Exception as e:
        logger.error(f"Error listando configuraciones de correo: {e}")
        raise HTTPException(status_code=500, detail="No se pudieron obtener configuraciones")


@app.post("/email-configs")
async def create_email_config(config: MultiEmailConfig, user: Dict[str, Any] = Depends(_get_current_user)):
    try:
        # Validar que password esté presente al crear
        if not config.password:
            raise HTTPException(status_code=400, detail="La contraseña es obligatoria al crear una cuenta")
        cfg_dict = config.model_dump()
        cfg_id = db_create_config(cfg_dict, owner_email=(user.get('email') or '').lower())
        return {"success": True, "id": cfg_id}
    except Exception as e:
        logger.error(f"Error creando configuración de correo: {e}")
        raise HTTPException(status_code=500, detail="No se pudo crear configuración")


@app.put("/email-configs/{config_id}")
async def update_email_config(config_id: str, config: MultiEmailConfig, user: Dict[str, Any] = Depends(_get_current_user)):
    try:
        update_data = config.model_dump()
        # Evitar sobreescribir password a null si no se envía
        if update_data.get("password") in (None, ""):
            update_data.pop("password", None)
        ok = db_update_config(config_id, update_data, owner_email=(user.get('email') or '').lower())
        if not ok:
            raise HTTPException(status_code=404, detail="Configuración no encontrada")
        return {"success": True, "id": config_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error actualizando configuración de correo: {e}")
        raise HTTPException(status_code=500, detail="No se pudo actualizar configuración")


@app.delete("/email-configs/{config_id}")
async def delete_email_config(config_id: str, user: Dict[str, Any] = Depends(_get_current_user)):
    try:
        ok = db_delete_config(config_id, owner_email=(user.get('email') or '').lower())
        if not ok:
            raise HTTPException(status_code=404, detail="Configuración no encontrada")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error eliminando configuración de correo: {e}")
        raise HTTPException(status_code=500, detail="No se pudo eliminar configuración")


@app.patch("/email-configs/{config_id}/enabled")
async def set_email_config_enabled(config_id: str, payload: ToggleEnabledPayload, user: Dict[str, Any] = Depends(_get_current_user)):
    try:
        ok = db_set_enabled(config_id, bool(payload.enabled), owner_email=(user.get('email') or '').lower())
        if not ok:
            raise HTTPException(status_code=404, detail="Configuración no encontrada")
        return {"success": True, "enabled": bool(payload.enabled)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error actualizando 'enabled' de configuración: {e}")
        raise HTTPException(status_code=500, detail="No se pudo actualizar el estado")


@app.post("/email-configs/{config_id}/toggle")
async def toggle_email_config_enabled(config_id: str, user: Dict[str, Any] = Depends(_get_current_user)):
    try:
        new_val = db_toggle_enabled(config_id, owner_email=(user.get('email') or '').lower())
        if new_val is None:
            raise HTTPException(status_code=404, detail="Configuración no encontrada")
        return {"success": True, "enabled": new_val}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error alternando 'enabled' de configuración: {e}")
        raise HTTPException(status_code=500, detail="No se pudo alternar el estado")


@app.get("/health")
async def health_check():
    """
    Health check endpoint for container health checks.
    
    Returns:
        dict: Simple health status.
    """
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.get("/status")
async def get_status(user: Dict[str, Any] = Depends(_get_current_user)):
    """
    Obtiene el estado actual del sistema.
    
    Returns:
        dict: Estado del sistema.
    """
    try:
        # Excel deshabilitado: valores fijos
        excel_files = []
        excel_exists = False
        last_modified = None

        # Estado del job
        job_status = invoice_sync.get_job_status()
        
        # Configuraciones de correo (desde MongoDB)
        try:
            email_configs = db_list_configs(include_password=False, owner_email=(user.get('email') or '').lower())
        except Exception as _e:
            logger.warning(f"No se pudieron obtener configuraciones de correo desde MongoDB: {_e}")
            email_configs = []
        
        status_info = {
            "status": "active",
            "excel_files_count": 0,
            "excel_exists": False,
            "last_modified": None,
            "temp_dir": settings.TEMP_PDF_DIR,
            "email_configs_count": len(email_configs),
            "email_configured": len([c for c in email_configs if c.get('username')]) > 0,
            "openai_configured": bool(settings.OPENAI_API_KEY),
            "job": {
                "running": job_status.running,
                "interval_minutes": job_status.interval_minutes,
                "next_run": job_status.next_run,
                "last_run": job_status.last_run
            },
            "excel_files": []
        }
        
        return status_info
        
    except Exception as e:
        logger.error(f"Error al obtener estado: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error al obtener estado: {str(e)}")

# -----------------------------
# V2 Invoices (headers + items)
# -----------------------------

@app.get("/v2/invoices/headers")
async def v2_list_headers(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    ruc_emisor: Optional[str] = None,
    ruc_receptor: Optional[str] = None,
    year_month: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    search: Optional[str] = None,
    user: Dict[str, Any] = Depends(_get_current_user),
):
    try:
        repo = MongoInvoiceRepository()
        coll = repo._headers()
        q = {}
        if ruc_emisor:
            q["emisor.ruc"] = ruc_emisor
        if ruc_receptor:
            q["receptor.ruc"] = ruc_receptor
        if year_month:
            q["mes_proceso"] = year_month
        from datetime import datetime
        if date_from or date_to:
            rng = {}
            if date_from:
                try:
                    rng["$gte"] = datetime.fromisoformat(date_from)
                except Exception:
                    pass
            if date_to:
                try:
                    rng["$lte"] = datetime.fromisoformat(date_to)
                except Exception:
                    pass
            if rng:
                q["fecha_emision"] = rng
        if search:
            q["$or"] = [
                {"emisor.nombre": {"$regex": search, "$options": "i"}},
                {"receptor.nombre": {"$regex": search, "$options": "i"}},
                {"numero_documento": {"$regex": search, "$options": "i"}},
            ]
        # Restringir por usuario si multi-tenant
        if settings.MULTI_TENANT_ENFORCE:
            owner = (user.get('email') or '').lower()
            if owner:
                q['owner_email'] = owner
        total = coll.count_documents(q)
        cursor = coll.find(q).sort("fecha_emision", -1).skip((page-1)*page_size).limit(page_size)
        items = []
        for d in cursor:
            d["id"] = d.get("_id")
            d.pop("_id", None)
            items.append(d)
        return {"success": True, "page": page, "page_size": page_size, "total": total, "data": items}
    except Exception as e:
        logger.error(f"Error listando headers v2: {e}")
        raise HTTPException(status_code=500, detail="Error listando headers")

@app.get("/v2/invoices/{header_id}")
async def v2_get_invoice(header_id: str, user: Dict[str, Any] = Depends(_get_current_user)):
    try:
        repo = MongoInvoiceRepository()
        q = {"_id": header_id}
        if settings.MULTI_TENANT_ENFORCE:
            owner = (user.get('email') or '').lower()
            if owner:
                q['owner_email'] = owner
        h = repo._headers().find_one(q)
        if not h:
            raise HTTPException(status_code=404, detail="No encontrado")
        iq = {"header_id": header_id}
        if settings.MULTI_TENANT_ENFORCE:
            owner = (user.get('email') or '').lower()
            if owner:
                iq['owner_email'] = owner
        items = list(repo._items().find(iq).sort("linea", 1))
        h["id"] = h.get("_id")
        h.pop("_id", None)
        for it in items:
            it["id"] = str(it.get("_id"))
            it.pop("_id", None)
        return {"success": True, "header": h, "items": items}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error obteniendo invoice v2: {e}")
        raise HTTPException(status_code=500, detail="Error obteniendo invoice")

@app.get("/v2/invoices/items")
async def v2_list_items(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    header_id: Optional[str] = None,
    iva: Optional[int] = Query(default=None, description="0,5,10"),
    search: Optional[str] = None,
    year_month: Optional[str] = None,
    user: Dict[str, Any] = Depends(_get_current_user),
):
    try:
        repo = MongoInvoiceRepository()
        items_coll = repo._items()
        q: Dict[str, Any] = {}
        if header_id:
            q["header_id"] = header_id
        if iva is not None:
            try:
                q["iva"] = int(iva)
            except Exception:
                pass
        if search:
            q["descripcion"] = {"$regex": search, "$options": "i"}
        if settings.MULTI_TENANT_ENFORCE:
            owner = (user.get('email') or '').lower()
            if owner:
                q['owner_email'] = owner
        if year_month and not header_id:
            hq = {"mes_proceso": year_month}
            if settings.MULTI_TENANT_ENFORCE and (user.get('email')):
                hq['owner_email'] = (user.get('email') or '').lower()
            header_ids = [h["_id"] for h in repo._headers().find(hq, {"_id": 1})]
            if header_ids:
                q["header_id"] = {"$in": header_ids}
            else:
                return {"success": True, "page": page, "page_size": page_size, "total": 0, "data": []}
        total = items_coll.count_documents(q)
        cursor = items_coll.find(q).sort([("header_id", 1), ("linea", 1)]).skip((page-1)*page_size).limit(page_size)
        data = []
        for d in cursor:
            d["id"] = str(d.get("_id"))
            d.pop("_id", None)
            data.append(d)
        return {"success": True, "page": page, "page_size": page_size, "total": total, "data": data}
    except Exception as e:
        logger.error(f"Error listando items v2: {e}")
        raise HTTPException(status_code=500, detail="Error listando items")

@app.post("/job/start", response_model=JobStatus)
async def start_job():
    """
    Inicia el trabajo programado para procesar correos periódicamente.
    
    Returns:
        JobStatus: Estado del trabajo.
    """
    try:
        job_status = invoice_sync.start_scheduled_job()
        return job_status
    except Exception as e:
        logger.error(f"Error al iniciar el job: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error al iniciar el job: {str(e)}")

@app.post("/job/stop", response_model=JobStatus)
async def stop_job():
    """
    Detiene el trabajo programado.
    
    Returns:
        JobStatus: Estado del trabajo.
    """
    try:
        job_status = invoice_sync.stop_scheduled_job()
        return job_status
    except Exception as e:
        logger.error(f"Error al detener el job: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error al detener el job: {str(e)}")

@app.get("/job/status", response_model=JobStatus)
async def job_status():
    """
    Obtiene el estado actual del trabajo programado.
    
    Returns:
        JobStatus: Estado del trabajo.
    """
    return invoice_sync.get_job_status()

@app.post("/job/interval", response_model=JobStatus)
async def set_job_interval(payload: IntervalPayload):
    """Ajusta el intervalo (minutos) del job de automatización."""
    try:
        logger.info(f"🛠️ Ajustando intervalo de job a {payload.minutes} minutos")
        status = invoice_sync.update_job_interval(payload.minutes)
        logger.info(
            "✅ Intervalo actualizado: running=%s, interval=%s, next_run=%s, last_run=%s",
            getattr(status, 'running', False), getattr(status, 'interval_minutes', None),
            getattr(status, 'next_run', None), getattr(status, 'last_run', None)
        )
        return status
    except Exception as e:
        logger.error(f"Error al ajustar intervalo del job: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error al ajustar intervalo: {str(e)}")

@app.get("/cache/stats")
async def cache_stats():
    """
    Obtiene estadísticas del cache de OpenAI.
    
    Returns:
        dict: Estadísticas del cache.
    """
    try:
        if hasattr(invoice_sync.openai_processor, 'cache') and invoice_sync.openai_processor.cache:
            stats = invoice_sync.openai_processor.cache.get_cache_stats()
            return {
                "cache_enabled": True,
                **stats
            }
        else:
            return {"cache_enabled": False, "message": "Cache no habilitado"}
    except Exception as e:
        logger.error(f"Error obteniendo estadísticas del cache: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error obteniendo estadísticas del cache: {str(e)}")

@app.post("/cache/clear")
async def clear_cache(older_than_hours: Optional[int] = None):
    """
    Limpia el cache de OpenAI.
    
    Args:
        older_than_hours: Si se especifica, elimina solo cache más viejo que X horas
    
    Returns:
        dict: Resultado de la limpieza.
    """
    try:
        if hasattr(invoice_sync.openai_processor, 'cache') and invoice_sync.openai_processor.cache:
            files_removed = invoice_sync.openai_processor.cache.clear_cache(older_than_hours)
            return {
                "success": True,
                "files_removed": files_removed,
                "message": f"Cache limpiado: {files_removed} archivos eliminados"
            }
        else:
            return {"success": False, "message": "Cache no habilitado"}
    except Exception as e:
        logger.error(f"Error limpiando cache: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error limpiando cache: {str(e)}")

@app.get("/imap/pool/stats")
async def imap_pool_stats():
    """
    Obtiene estadísticas del pool de conexiones IMAP.
    
    Returns:
        dict: Estadísticas del pool de conexiones.
    """
    try:
        from app.modules.email_processor.connection_pool import get_imap_pool
        pool = get_imap_pool()
        stats = pool.get_pool_stats()
        
        return {
            "pool_enabled": True,
            "configurations": stats,
            "total_pools": len(stats)
        }
    except Exception as e:
        logger.error(f"Error obteniendo estadísticas del pool IMAP: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error obteniendo estadísticas del pool: {str(e)}")

@app.get("/excel/stats")
async def excel_stats():
    raise HTTPException(status_code=410, detail="Exportación a Excel deshabilitada")

@app.get("/health/detailed")
async def detailed_health():
    """
    Health check comprensivo con métricas detalladas de todos los componentes.
    
    Returns:
        dict: Estado detallado del sistema con métricas de performance.
    """
    try:
        from app.modules.monitoring import get_health_checker
        health_checker = get_health_checker()
        health_report = await health_checker.comprehensive_health_check()
        
        return health_report
    except Exception as e:
        logger.error(f"Error en health check detallado: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error en health check: {str(e)}")

@app.get("/health/trends")
async def health_trends():
    """
    Obtiene tendencias de salud del sistema basadas en histórico.
    
    Returns:
        dict: Tendencias y métricas históricas.
    """
    try:
        from app.modules.monitoring import get_health_checker
        health_checker = get_health_checker()
        trends = health_checker.get_health_trends()
        
        return trends
    except Exception as e:
        logger.error(f"Error obteniendo tendencias de salud: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error obteniendo tendencias: {str(e)}")

@app.post("/system/force-restart")
async def force_system_restart():
    """
    Endpoint de emergencia para forzar reinicio del sistema cuando hay bloqueos.
    
    Returns:
        dict: Confirmación de reinicio.
    """
    global invoice_sync
    
    try:
        logger.warning("🚨 REINICIO DE EMERGENCIA SOLICITADO - Forzando limpieza del sistema")
        
        # Detener job programado si está corriendo
        try:
            invoice_sync.stop_scheduled_job()
            logger.info("✅ Job programado detenido")
        except Exception as e:
            logger.warning(f"⚠️ Error deteniendo job: {e}")
        
        # Limpiar tareas pendientes
        try:
            task_queue.cleanup_old_tasks()
            logger.info("✅ Tareas limpiadas")
        except Exception as e:
            logger.warning(f"⚠️ Error limpiando tareas: {e}")
        
        # Liberar lock de procesamiento
        try:
            if PROCESSING_LOCK.locked():
                PROCESSING_LOCK.release()
                logger.info("✅ Processing lock liberado")
        except Exception as e:
            logger.warning(f"⚠️ Error liberando lock: {e}")
        
        # Reinicializar invoice_sync
        try:
            invoice_sync = CuenlyApp()
            logger.info("✅ CuenlyApp reinicializado")
        except Exception as e:
            logger.warning(f"⚠️ Error reinicializando CuenlyApp: {e}")
        
        return {
            "success": True,
            "message": "Sistema reiniciado exitosamente",
            "timestamp": datetime.now().isoformat(),
            "actions": [
                "Job programado detenido",
                "Tareas limpiadas", 
                "Processing lock liberado",
                "CuenlyApp reinicializado"
            ]
        }
    except Exception as e:
        logger.error(f"❌ Error en reinicio de emergencia: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error en reinicio: {str(e)}")

@app.get("/system/health")
async def get_system_health():
    """
    Endpoint de salud del sistema con información detallada.
    
    Returns:
        dict: Estado de salud del sistema.
    """
    try:
        import psutil
        import threading
        
        # Información básica del sistema
        health_info = {
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "uptime_seconds": time.time() - getattr(app.state, 'start_time', time.time()),
            
            # Estado de threads
            "active_threads": threading.active_count(),
            "thread_names": [t.name for t in threading.enumerate()],
            
            # Estado de procesamiento
            "processing_lock_acquired": PROCESSING_LOCK.locked(),
            "pending_tasks": task_queue.get_pending_tasks_count(),
            
            # Job programado
            "scheduled_job_running": invoice_sync.get_job_status().get("running", False),
            
            # Memoria y CPU
            "memory_usage_mb": psutil.Process().memory_info().rss / 1024 / 1024,
            "cpu_percent": psutil.Process().cpu_percent(),
        }
        
        # Determinar estado general
        if health_info["active_threads"] > 20:
            health_info["status"] = "warning"
            health_info["warning"] = "Alto número de threads activos"
        elif health_info["memory_usage_mb"] > 500:
            health_info["status"] = "warning"  
            health_info["warning"] = "Alto uso de memoria"
        elif health_info["processing_lock_acquired"] and health_info["pending_tasks"] == 0:
            health_info["status"] = "warning"
            health_info["warning"] = "Processing lock adquirido sin tareas pendientes"
            
        return health_info
        
    except Exception as e:
        logger.error(f"Error obteniendo salud del sistema: {str(e)}")
        return {
            "status": "error",
            "timestamp": datetime.now().isoformat(),
            "error": str(e)
        }

def start():
    """Inicia el servidor API."""
    # Guardar tiempo de inicio
    app.state.start_time = time.time()
    
    uvicorn.run(
        "app.api.api:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=True
    )

if __name__ == "__main__":
    start()

# -----------------------------
# Preferencias (UI / Auto‑refresh)
# -----------------------------

@app.get("/prefs/auto-refresh", response_model=AutoRefreshPref)
async def get_auto_refresh(uid: Optional[str] = Query(default="global")):
    try:
        data = prefs_get_auto_refresh(uid) or {"enabled": False, "interval_ms": 30000}
        return AutoRefreshPref(uid=uid, enabled=bool(data.get("enabled", False)), interval_ms=int(data.get("interval_ms", 30000)))
    except Exception as e:
        logger.error(f"Error al obtener preferencia auto-refresh: {e}")
        raise HTTPException(status_code=500, detail="No se pudo obtener preferencia")

@app.post("/prefs/auto-refresh", response_model=AutoRefreshPref)
async def set_auto_refresh(payload: AutoRefreshPayload):
    try:
        uid = payload.uid or "global"
        data = prefs_set_auto_refresh(uid, payload.enabled, payload.interval_ms)
        return AutoRefreshPref(uid=uid, enabled=bool(data.get("enabled", False)), interval_ms=int(data.get("interval_ms", 30000)))
    except Exception as e:
        logger.error(f"Error al guardar preferencia auto-refresh: {e}")
        raise HTTPException(status_code=500, detail="No se pudo guardar preferencia")

# -----------------------------
# Exportadores Avanzados 
# -----------------------------

@app.post("/export/excel-completo")
async def export_excel_completo(background_tasks: BackgroundTasks, run_async: bool = False):
    raise HTTPException(status_code=410, detail="Exportación a Excel deshabilitada")

@app.post("/export/mongodb")
async def export_to_mongodb(background_tasks: BackgroundTasks, run_async: bool = False):
    """
    Exporta TODAS las facturas a MongoDB en formato documental optimizado.
    Ideal para análisis avanzado, reporting y consultas complejas.
    """
    try:
        if run_async:
            background_tasks.add_task(_export_mongodb_task)
            return {
                "success": True,
                "message": "Exportación a MongoDB iniciada en segundo plano",
                "export_type": "mongodb"
            }
        else:
            return await _export_mongodb_task()
    except Exception as e:
        logger.error(f"Error en export MongoDB: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error en export MongoDB: {str(e)}")

@app.get("/export/excel-completo/list")
async def list_excel_completo_files():
    raise HTTPException(status_code=410, detail="Exportación a Excel deshabilitada")

@app.get("/export/excel-completo/{year_month}")
async def download_excel_completo(year_month: str):
    raise HTTPException(status_code=410, detail="Exportación a Excel deshabilitada")

@app.post("/export/excel-completo/{year_month}")
async def export_excel_completo_month(year_month: str, background_tasks: BackgroundTasks, run_async: bool = False):
    """
    Exporta facturas de un mes específico desde MongoDB al formato Excel completo.
    Args:
        year_month: Mes en formato YYYY-MM (ej: 2025-01)
        run_async: Si true, ejecuta en segundo plano
    """
    try:
        # Validar formato de fecha
        try:
            datetime.strptime(year_month, "%Y-%m")
        except ValueError:
            raise HTTPException(status_code=400, detail="Formato de mes inválido. Use YYYY-MM")
        
        if run_async:
            background_tasks.add_task(_export_completo_month_task, year_month)
            return {
                "success": True,
                "message": f"Exportación completa del mes {year_month} iniciada en segundo plano",
                "export_type": "excel_completo",
                "year_month": year_month
            }
        else:
            return await _export_completo_month_task(year_month)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error en export completo por mes: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error en export completo: {str(e)}")

@app.get("/export/mongodb/stats")
async def mongodb_export_stats():
    """
    Obtiene estadísticas de la base de datos MongoDB.
    """
    try:
        exporter = MongoDBExporter()
        try:
            stats = exporter.get_statistics()
            return {
                "success": True,
                "export_type": "mongodb",
                "database_stats": stats
            }
        finally:
            exporter.close_connections()
    except Exception as e:
        logger.error(f"Error obteniendo stats MongoDB: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error obteniendo estadísticas: {str(e)}")

@app.post("/export/process-and-export")
async def process_and_export_all(
    background_tasks: BackgroundTasks,
    export_types: List[str] = Query(default=["mongodb"], description="Tipos de export soportados"),
    run_async: bool = False
):
    raise HTTPException(status_code=410, detail="Exportación a Excel deshabilitada")

# Funciones auxiliares para tareas en segundo plano

async def _export_completo_task():
    return {"success": False, "message": "Exportación a Excel deshabilitada"}

async def _export_mongodb_task():
    """Tarea para exportar a MongoDB"""
    try:
        # Obtener facturas para exportar
        invoices = getattr(invoice_sync, '_last_processed_invoices', [])
        
        if not invoices:
            return {
                "success": False,
                "message": "No hay facturas disponibles para exportar a MongoDB. Procese emails primero.",
                "export_type": "mongodb"
            }
        
        exporter = MongoDBExporter()
        try:
            result = exporter.export_invoices(invoices)
            
            return {
                "success": True,
                "message": f"Exportación a MongoDB completada: {result['inserted']} insertados, {result['updated']} actualizados",
                "export_type": "mongodb",
                "mongo_result": result,
                "invoice_count": len(invoices)
            }
        finally:
            exporter.close_connections()
            
    except Exception as e:
        logger.error(f"Error en export MongoDB task: {e}")
        return {
            "success": False,
            "message": f"Error en exportación MongoDB: {str(e)}",
            "export_type": "mongodb"
        }

async def _process_and_export_task(export_types: List[str]):
    return {"success": False, "message": "Exportación deshabilitada"}

async def _export_completo_month_task(year_month: str):
    return {"success": False, "message": "Exportación a Excel deshabilitada"}

# -----------------------------
# Consultas MongoDB y Exports por Fecha
# -----------------------------

@app.get("/invoices/months")
async def get_available_months(user: Dict[str, Any] = Depends(_get_current_user)):
    """
    Obtiene lista de meses disponibles con estadísticas básicas desde MongoDB.
    """
    try:
        query_service = get_mongo_query_service()
        owner = (user.get('email') or '').lower() if settings.MULTI_TENANT_ENFORCE else None
        months = query_service.get_available_months(owner_email=owner)
        
        return {
            "success": True,
            "months": months,
            "total_months": len(months)
        }
    except Exception as e:
        logger.error(f"Error obteniendo meses disponibles: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error obteniendo meses: {str(e)}")

@app.get("/invoices/month/{year_month}")
async def get_invoices_by_month(year_month: str, user: Dict[str, Any] = Depends(_get_current_user)):
    """
    Obtiene todas las facturas de un mes específico desde MongoDB.
    
    Args:
        year_month: Mes en formato YYYY-MM
    """
    try:
        # Validar formato
        try:
            datetime.strptime(year_month, "%Y-%m")
        except ValueError:
            raise HTTPException(status_code=400, detail="Formato de mes incorrecto. Use YYYY-MM")
        
        query_service = get_mongo_query_service()
        owner = (user.get('email') or '').lower() if settings.MULTI_TENANT_ENFORCE else None
        invoices = query_service.get_invoices_by_month(year_month, owner_email=owner)
        
        return {
            "success": True,
            "year_month": year_month,
            "invoices": invoices,
            "count": len(invoices)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error obteniendo facturas del mes {year_month}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error obteniendo facturas: {str(e)}")

@app.get("/invoices/month/{year_month}/stats")
async def get_month_statistics(request: Request, year_month: str, user: Dict[str, Any] = Depends(_get_current_user)):
    """
    Obtiene estadísticas detalladas de un mes específico desde MongoDB.
    """
    client_ip = request.client.host if request.client else "unknown"
    
    try:
        # Validación de seguridad
        try:
            SecurityValidators.validate_year_month(year_month)
        except ValidationError as e:
            log_security_event("validation_error", {"error": str(e), "year_month": year_month}, client_ip)
            raise HTTPException(status_code=400, detail=str(e))
        
        query_service = get_mongo_query_service()
        owner = (user.get('email') or '').lower() if settings.MULTI_TENANT_ENFORCE else None
        stats = query_service.get_month_statistics(year_month, owner_email=owner)
        
        # Log acceso a estadísticas
        logger.info(f"📊 Stats solicitadas para {year_month} por IP {client_ip}")
        
        return {
            "success": True,
            "statistics": stats
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error obteniendo estadísticas del mes {year_month}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error obteniendo estadísticas: {str(e)}")

@app.post("/invoices/search")
async def search_invoices(
    query: str = Query(default="", description="Texto libre para buscar"),
    start_date: Optional[str] = Query(default=None, description="Fecha inicio YYYY-MM-DD"),
    end_date: Optional[str] = Query(default=None, description="Fecha fin YYYY-MM-DD"),
    provider_ruc: Optional[str] = Query(default=None, description="RUC del proveedor"),
    client_ruc: Optional[str] = Query(default=None, description="RUC del cliente"),
    min_amount: Optional[float] = Query(default=None, description="Monto mínimo"),
    max_amount: Optional[float] = Query(default=None, description="Monto máximo"),
    limit: int = Query(default=100, description="Límite de resultados")
):
    """
    Búsqueda avanzada de facturas en MongoDB con múltiples filtros.
    """
    try:
        query_service = get_mongo_query_service()
        results = query_service.search_invoices(
            query=query,
            start_date=start_date,
            end_date=end_date,
            provider_ruc=provider_ruc,
            client_ruc=client_ruc,
            min_amount=min_amount,
            max_amount=max_amount,
            limit=limit
        )
        
        return {
            "success": True,
            "results": results,
            "count": len(results),
            "limit": limit
        }
    except Exception as e:
        logger.error(f"Error en búsqueda de facturas: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error en búsqueda: {str(e)}")

@app.get("/invoices/recent-activity")
async def get_recent_activity(days: int = Query(default=7, description="Días hacia atrás")):
    """
    Obtiene actividad reciente del sistema desde MongoDB.
    """
    try:
        query_service = get_mongo_query_service()
        activity = query_service.get_recent_activity(days)
        
        return {
            "success": True,
            "activity": activity
        }
    except Exception as e:
        logger.error(f"Error obteniendo actividad reciente: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error obteniendo actividad: {str(e)}")

@app.get("/export/excel-from-mongodb/{year_month}")
async def export_excel_from_mongodb(request: Request,
                                   year_month: str, 
                                   export_type: str = Query(default="completo", 
                                                          description="Tipo de export: ascont, completo")):
    raise HTTPException(status_code=410, detail="Exportación a Excel deshabilitada")

def _mongo_doc_to_invoice_data(doc: Dict[str, Any]) -> InvoiceData:
    """
    Convierte documento MongoDB a InvoiceData para compatibilidad con exportadores existentes.
    """
    try:
        # Extraer datos principales
        factura = doc.get("factura", {})
        emisor = doc.get("emisor", {})
        receptor = doc.get("receptor", {})
        montos = doc.get("montos", {})
        productos = doc.get("productos", [])
        
        # Convertir fecha
        fecha = None
        if factura.get("fecha"):
            try:
                fecha = datetime.fromisoformat(factura["fecha"].replace("Z", "+00:00"))
            except:
                pass
        
        # Crear InvoiceData
        invoice = InvoiceData(
            numero_factura=factura.get("numero", ""),
            fecha=fecha,
            ruc_emisor=emisor.get("ruc", ""),
            nombre_emisor=emisor.get("nombre", ""),
            ruc_cliente=receptor.get("ruc", ""),
            nombre_cliente=receptor.get("nombre", ""),
            email_cliente=receptor.get("email", ""),
            monto_total=montos.get("monto_total", 0),
            subtotal_exentas=montos.get("subtotal_exentas", 0),
            subtotal_5=montos.get("subtotal_5", 0),
            subtotal_10=montos.get("subtotal_10", 0),
            iva_5=montos.get("iva_5", 0),
            iva_10=montos.get("iva_10", 0),
            iva=montos.get("total_iva", 0)
        )
        
        # Agregar campos adicionales si están disponibles
        if "datos_tecnicos" in doc:
            datos_tec = doc["datos_tecnicos"]
            invoice.cdc = datos_tec.get("cdc", "")
            invoice.timbrado = datos_tec.get("timbrado", "")
        
        # Agregar metadata
        if "metadata" in doc:
            metadata = doc["metadata"]
            invoice.email_origen = metadata.get("email_origen", "")
            invoice.mes_proceso = doc.get("indices", {}).get("year_month", "")
        
        return invoice
        
    except Exception as e:
        logger.error(f"Error convirtiendo documento MongoDB: {e}")
        # Retornar InvoiceData mínimo en caso de error
        return InvoiceData(
            numero_factura=doc.get("factura_id", "ERROR"),
            fecha=datetime.now(),
            ruc_emisor="",
            nombre_emisor="Error en conversión",
            ruc_cliente="",
            nombre_cliente="",
            email_cliente="",
            monto_total=0
        )
