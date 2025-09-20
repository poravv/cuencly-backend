import os
import logging
import time
import signal
import threading
from typing import List, Dict, Any, Optional
import argparse
from datetime import datetime

from app.config.settings import settings
from app.config.export_config import get_mongodb_config
from app.models.models import InvoiceData, ProcessResult, EmailConfig, JobStatus
from app.modules.email_processor.email_processor import MultiEmailProcessor, EmailProcessor
from app.modules.openai_processor.openai_processor import OpenAIProcessor
from app.modules.mongo_exporter import MongoDBExporter
from app.modules.scheduler.processing_lock import PROCESSING_LOCK

# Configurar logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("cuenlyapp.log")
    ]
)

logger = logging.getLogger(__name__)

class CuenlyApp:
    def __init__(self):
        """Inicializa el sistema de sincronización de facturas usando OpenAI."""
        # Crear directorios necesarios
        os.makedirs(settings.TEMP_PDF_DIR, exist_ok=True)
        # Directorio de Excel eliminado del flujo
        
        # Inicializar componentes
        # Usar MultiEmailProcessor si hay múltiples configuraciones de correo (desde MongoDB)
        try:
            from app.modules.email_processor.config_store import get_enabled_configs
            email_configs = get_enabled_configs(include_password=True)
        except Exception as e:
            logger.warning(f"No se pudieron cargar configuraciones de correo desde MongoDB: {e}")
            email_configs = []

        if len(email_configs) > 1:
            self.email_processor = MultiEmailProcessor()
            logger.info(f"Usando MultiEmailProcessor con {len(email_configs)} cuentas de correo")
        elif len(email_configs) == 1:
            # EmailProcessor sin args carga primera config habilitada desde MongoDB
            self.email_processor = EmailProcessor()
            logger.info("Usando EmailProcessor para una sola cuenta de correo (MongoDB)")
        else:
            # Sin configuraciones: permitir inicialización y esperar que el usuario configure vía UI
            self.email_processor = MultiEmailProcessor()
            logger.info("No hay cuentas configuradas aún. MultiEmailProcessor inicializado sin cuentas")
        
        self.openai_processor = OpenAIProcessor()
        
        # Inicializar MongoDB como almacenamiento primario
        mongodb_config = get_mongodb_config()
        if mongodb_config.get("as_primary", True):
            self.mongodb_exporter = MongoDBExporter()
            logger.info("✅ MongoDB configurado como almacenamiento primario")
        else:
            self.mongodb_exporter = None
            logger.info("⚠️ MongoDB no configurado como primario")
        
        # Guardar referencia a últimas facturas procesadas
        self._last_processed_invoices: List[InvoiceData] = []
        
        # Estado del job
        self._job_status = JobStatus(
            running=False,
            interval_minutes=settings.JOB_INTERVAL_MINUTES,
            next_run=None,
            last_run=None,
            last_result=None
        )
        
        logger.info("Sistema CuenlyApp inicializado correctamente")
    
    def process_emails(self) -> ProcessResult:
        """
        Procesa correos electrónicos para extraer facturas con timeout de seguridad.
        
        Returns:
            ProcessResult: Resultado del procesamiento.
        """
        logger.info("🚀 Iniciando procesamiento de correos con watchdog de seguridad")
        
        # Registrar inicio del procesamiento
        self._job_status.last_run = datetime.now().isoformat()
        
        # Usar watchdog para evitar cuelgues indefinidos
        import queue
        result_queue = queue.Queue()
        
        def process_with_timeout():
            try:
                # Procesar correos serializadamente para evitar colisiones
                with PROCESSING_LOCK:
                    if hasattr(self.email_processor, 'process_all_emails'):
                        result = self.email_processor.process_all_emails()
                    else:
                        result = self.email_processor.process_emails()
                
                result_queue.put(('success', result))
                
            except Exception as e:
                logger.error(f"❌ Error en procesamiento: {e}")
                result_queue.put(('error', str(e)))
        
        # Ejecutar procesamiento en thread separado con timeout global
        process_thread = threading.Thread(target=process_with_timeout, daemon=True)
        process_thread.start()
        
        # Timeout global de 10 minutos para todo el procesamiento
        process_thread.join(timeout=600)
        
        if process_thread.is_alive():
            logger.error("❌ TIMEOUT GLOBAL: El procesamiento tomó más de 10 minutos. Abortando...")
            result = ProcessResult(
                success=False,
                message="Timeout global: El procesamiento fue abortado por seguridad (>10 min)",
                invoice_count=0,
                invoices=[]
            )
        else:
            # Obtener resultado del thread
            try:
                result_type, result_data = result_queue.get_nowait()
                if result_type == 'success':
                    result = result_data
                    
                    # **NUEVO**: Exportar automáticamente a MongoDB si está configurado
                    if result.success and result.invoices and self.mongodb_exporter:
                        try:
                            logger.info("💾 Exportando automáticamente a MongoDB...")
                            mongo_result = self.mongodb_exporter.export_invoices(result.invoices)
                            
                            # Guardar referencia para otros exportadores
                            self._last_processed_invoices = result.invoices
                            
                            # Actualizar mensaje del resultado
                            if mongo_result and mongo_result.get('inserted', 0) + mongo_result.get('updated', 0) > 0:
                                result.message += f" | MongoDB: {mongo_result['inserted']} insertados, {mongo_result['updated']} actualizados"
                                logger.info("✅ Exportación a MongoDB completada: %s", mongo_result)
                            else:
                                logger.warning("⚠️ MongoDB export devolvió resultado vacío")
                                
                        except Exception as mongo_error:
                            logger.error("❌ Error exportando a MongoDB: %s", mongo_error)
                            # No fallar el proceso completo por error de MongoDB
                            result.message += f" | ⚠️ MongoDB export falló: {str(mongo_error)}"
                        finally:
                            # Cerrar conexiones MongoDB
                            if self.mongodb_exporter:
                                self.mongodb_exporter.close_connections()
                    
                else:
                    result = ProcessResult(
                        success=False,
                        message=f"Error en procesamiento: {result_data}",
                        invoice_count=0,
                        invoices=[]
                    )
            except queue.Empty:
                result = ProcessResult(
                    success=False,
                    message="Error: No se pudo obtener resultado del procesamiento",
                    invoice_count=0,
                    invoices=[]
                )
        
        # Actualizar estado del job
        self._job_status.last_result = result
        
        return result

    def process_pdf(self, pdf_path: str, metadata: Dict[str, Any] = None) -> InvoiceData:
        """
        Procesa un archivo PDF para extraer datos de factura.
        
        Args:
            pdf_path: Ruta al archivo PDF.
            metadata: Metadatos adicionales.
            
        Returns:
            InvoiceData: Datos extraídos de la factura.
        """
        logger.info(f"Procesando PDF: {pdf_path}")
        # Serializar extracción para mantener coherencia con export posterior
        with PROCESSING_LOCK:
            invoice_data = self.openai_processor.extract_invoice_data(pdf_path, metadata)
            
            # **NUEVO**: Exportar automáticamente a MongoDB si está configurado
            if invoice_data and self.mongodb_exporter:
                try:
                    logger.info("💾 Exportando PDF procesado a MongoDB...")
                    mongo_result = self.mongodb_exporter.export_invoices([invoice_data])
                    
                    # Guardar en referencia
                    self._last_processed_invoices = [invoice_data]
                    
                    logger.info("✅ PDF exportado a MongoDB: %s", mongo_result)
                except Exception as mongo_error:
                    logger.error("❌ Error exportando PDF a MongoDB: %s", mongo_error)
                finally:
                    # Cerrar conexiones
                    if self.mongodb_exporter:
                        self.mongodb_exporter.close_connections()
            
            return invoice_data
    
    def start_scheduled_job(self) -> JobStatus:
        """
        Inicia el trabajo programado para procesar correos periódicamente.
        
        Returns:
            JobStatus: Estado actual del trabajo.
        """
        if not self._job_status.running:
            self.email_processor.start_scheduled_job()
            self._job_status.running = True
            self._job_status.next_run = self._calculate_next_run()
            logger.info(f"Job programado iniciado. Próxima ejecución: {self._job_status.next_run}")
        
        return self._job_status
    
    def stop_scheduled_job(self) -> JobStatus:
        """
        Detiene el trabajo programado.
        
        Returns:
            JobStatus: Estado actual del trabajo.
        """
        if self._job_status.running:
            self.email_processor.stop_scheduled_job()
            self._job_status.running = False
            self._job_status.next_run = None
            logger.info("Job programado detenido")
        
        return self._job_status
    
    def get_job_status(self) -> JobStatus:
        """
        Obtiene el estado actual del trabajo programado.
        
        Returns:
            JobStatus: Estado actual del trabajo.
        """
        # Preferir estado real reportado por el scheduler si está disponible
        try:
            if hasattr(self.email_processor, 'scheduled_job_status'):
                sched = self.email_processor.scheduled_job_status()
                if isinstance(sched, dict) and sched:
                    self._job_status.running = bool(sched.get('running', False))
                    # Convertir next_run y last_run a ISO
                    def _to_iso(v):
                        from datetime import datetime
                        try:
                            if v is None:
                                return None
                            if isinstance(v, (int, float)):
                                return datetime.fromtimestamp(v).isoformat()
                            # ya es iso o datetime string
                            return str(v)
                        except Exception:
                            return None
                    next_iso = _to_iso(sched.get('next_run'))
                    last_iso = _to_iso(sched.get('last_run'))
                    self._job_status.next_run = next_iso
                    self._job_status.last_run = last_iso
                    # También timestamps en epoch
                    from datetime import datetime
                    try:
                        self._job_status.next_run_ts = int(datetime.fromisoformat(next_iso).timestamp()) if next_iso else None
                    except Exception:
                        self._job_status.next_run_ts = None
                    try:
                        self._job_status.last_run_ts = int(datetime.fromisoformat(last_iso).timestamp()) if last_iso else None
                    except Exception:
                        self._job_status.last_run_ts = None
                    self._job_status.interval_minutes = int(sched.get('interval_minutes', self._job_status.interval_minutes))
                    # last_result si viene como ProcessResult compatible
                    lr = sched.get('last_result')
                    try:
                        self._job_status.last_result = lr if lr is None else lr
                    except Exception:
                        pass
            else:
                # Estimación si no hay estado del scheduler
                if self._job_status.running:
                    next_iso = self._calculate_next_run()
                    self._job_status.next_run = next_iso
                    try:
                        from datetime import datetime
                        self._job_status.next_run_ts = int(datetime.fromisoformat(next_iso).timestamp()) if next_iso else None
                    except Exception:
                        self._job_status.next_run_ts = None
        except Exception:
            # Respaldo: estimación simple
            if self._job_status.running:
                next_iso = self._calculate_next_run()
                self._job_status.next_run = next_iso
                try:
                    from datetime import datetime
                    self._job_status.next_run_ts = int(datetime.fromisoformat(next_iso).timestamp()) if next_iso else None
                except Exception:
                    self._job_status.next_run_ts = None
        
        return self._job_status

    def update_job_interval(self, minutes: int) -> JobStatus:
        try:
            minutes = max(1, int(minutes))
        except Exception:
            minutes = self._job_status.interval_minutes

        # delegar al procesador subyacente
        if hasattr(self.email_processor, 'set_interval_minutes'):
            self.email_processor.set_interval_minutes(minutes)
        # actualizar estado interno
        self._job_status.interval_minutes = minutes
        return self.get_job_status()
    
    def _calculate_next_run(self) -> str:
        """
        Calcula el tiempo de la próxima ejecución del job.
        
        Returns:
            str: Tiempo de la próxima ejecución en formato ISO.
        """
        # Estimación simple basada en el intervalo actualmente reportado por el job
        now = datetime.now()
        next_run = now.replace(second=0, microsecond=0)
        
        # Añadir los minutos del intervalo (preferir el estado interno si existe)
        from datetime import timedelta
        interval = getattr(self._job_status, 'interval_minutes', None) or settings.JOB_INTERVAL_MINUTES
        try:
            interval = max(1, int(interval))
        except Exception:
            interval = settings.JOB_INTERVAL_MINUTES
        next_run += timedelta(minutes=interval)
        
        return next_run.isoformat()

def main():
    """Función principal para ejecutar desde línea de comandos."""
    parser = argparse.ArgumentParser(description="CuenlyApp: Sincronización de facturas desde correo")
    parser.add_argument("--process", action="store_true", help="Procesar correos")
    parser.add_argument("--start-job", action="store_true", help="Iniciar job programado")
    parser.add_argument("--stop-job", action="store_true", help="Detener job programado")
    parser.add_argument("--status", action="store_true", help="Mostrar estado")
    
    args = parser.parse_args()
    
    cuenlyapp = CuenlyApp()
    
    if args.process:
        result = cuenlyapp.process_emails()
        print(f"Resultado: {result.success}")
        print(f"Mensaje: {result.message}")
        print(f"Facturas procesadas: {result.invoice_count}")
    
    elif args.start_job:
        status = cuenlyapp.start_scheduled_job()
        print(f"Job iniciado: {status.running}")
        print(f"Próxima ejecución: {status.next_run}")
        
        # Mantener proceso vivo
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("Deteniendo job...")
            cuenlyapp.stop_scheduled_job()
    
    elif args.stop_job:
        status = cuenlyapp.stop_scheduled_job()
        print(f"Job detenido: {not status.running}")
    
    elif args.status:
        status = cuenlyapp.get_job_status()
        print(f"Job activo: {status.running}")
        print(f"Próxima ejecución: {status.next_run}")
        print(f"Última ejecución: {status.last_run}")
        if status.last_result:
            print(f"Último resultado: {status.last_result.message}")
    
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
