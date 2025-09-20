import os
import re
import time
import threading
import logging
import schedule
import queue
import pickle
import email.utils
from typing import List, Tuple, Dict, Any, Optional
from datetime import datetime

from app.config.settings import settings
from app.models.models import EmailConfig, MultiEmailConfig, InvoiceData, ProcessResult
from app.modules.openai_processor.openai_processor import OpenAIProcessor
from app.repositories.mongo_invoice_repository import MongoInvoiceRepository
from app.modules.mapping.invoice_mapping import map_invoice


from app.modules.email_processor.errors import OpenAIFatalError, OpenAIRetryableError

from .imap_client import IMAPClient, decode_mime_header
from .link_extractor import extract_links_from_message
from .downloader import download_pdf_from_url
from .storage import save_binary, sanitize_filename, ensure_dirs
from .connection_pool import get_imap_pool
from .config_store import get_enabled_configs


from .dedup import deduplicate_invoices

# NUEVO runner thread-safe (sincroniza con tu API /job/start)
from app.modules.scheduler.job_runner import ScheduledJobRunner

logger = logging.getLogger(__name__)


# =========================
#  MultiEmailProcessor
# =========================
class MultiEmailProcessor:
    """
    Orquestador multi-cuenta. Mantiene la funcionalidad anterior pero delega
    en EmailProcessor para cada cuenta. Incluye job programado.
    """
    def __init__(self, email_configs: List[MultiEmailConfig] = None, owner_email: Optional[str] = None):
        if email_configs is None:
            try:
                configs_data = get_enabled_configs(include_password=True)
            except Exception as e:
                logger.error(f"Error cargando configuraciones de correo desde MongoDB: {e}")
                configs_data = []
            self.email_configs = [MultiEmailConfig(**cfg) for cfg in configs_data]
        else:
            self.email_configs = email_configs

        self.openai_processor = OpenAIProcessor()
        self.owner_email = (owner_email or '').lower() if owner_email else ''

        ensure_dirs()

        # Scheduler legado (basado en 'schedule')
        self._job_running = False
        self._job_thread: Optional[threading.Thread] = None

        # NUEVO: runner moderno para API /job/start|/job/stop
        self._scheduler: Optional[ScheduledJobRunner] = None

        logger.info(f"MultiEmailProcessor inicializado con {len(self.email_configs)} cuentas de correo")

    def process_all(self) -> Dict[str, Any]:
        """Método breve para un endpoint /run-once (compat)."""
        res = self.process_all_emails()
        return res.dict() if hasattr(res, "dict") else {
            "success": res.success, "message": res.message, "invoice_count": res.invoice_count
        }

    def _remove_duplicate_invoices(self, invoices: List[InvoiceData]) -> List[InvoiceData]:
        return deduplicate_invoices(invoices)

    def process_all_emails(self) -> ProcessResult:
        # Refrescar configuración en cada corrida para reflejar cambios dinámicos desde el frontend
        try:
            configs_data = get_enabled_configs(include_password=True)
            self.email_configs = [MultiEmailConfig(**cfg) for cfg in configs_data]
        except Exception as e:
            logger.warning(f"No se pudo refrescar configuraciones desde MongoDB: {e}")
        all_invoices: List[InvoiceData] = []
        success_count = 0
        errors: List[str] = []
        

        logger.info(f"Iniciando procesamiento de {len(self.email_configs)} cuentas de correo")

        if not self.email_configs:
            return ProcessResult(
                success=False,
                message="No hay cuentas de correo configuradas. Agregue al menos una desde la UI.",
                invoice_count=0,
                invoices=[]
            )

        for idx, cfg in enumerate(self.email_configs):
            logger.info(f"Procesando cuenta {idx + 1}/{len(self.email_configs)}: {cfg.username}")
            try:
                # Usar threading con timeout para evitar bloqueos indefinidos
                import threading
                import queue
                import signal
                
                result_queue = queue.Queue()
                
                def process_account():
                    try:
                        single = EmailProcessor(EmailConfig(
                            host=cfg.host, port=cfg.port, username=cfg.username, password=cfg.password,
                            search_criteria=cfg.search_criteria, search_terms=cfg.search_terms or []
                        ))
                        r = single.process_emails()
                        # Serializar con pickle para preservar objetos complejos
                        result_queue.put(pickle.dumps(('success', r)))
                    except Exception as e:
                        result_queue.put(pickle.dumps(('error', str(e))))
                
                # Ejecutar en thread separado con timeout más largo
                thread = threading.Thread(target=process_account, daemon=True)
                thread.start()
                
                # Timeout de 180 segundos (3 minutos) por cuenta
                thread.join(timeout=180)
                
                if thread.is_alive():
                    # Thread aún ejecutándose - timeout
                    errors.append(f"Timeout en {cfg.username}: procesamiento tomó más de 180 segundos")
                    logger.error(f"❌ Timeout al procesar cuenta {cfg.username}: procesamiento tomó más de 180 segundos")
                    # Forzar terminación del thread (no es ideal pero evita cuelgues)
                    continue
                
                # Obtener resultado
                try:
                    pickled_result = result_queue.get_nowait()
                    result_type, result_data = pickle.loads(pickled_result)
                    if result_type == 'success':
                        r = result_data
                        if r.success:
                            success_count += 1
                            # Validar que las facturas sean objetos correctos
                            valid_invoices = []
                            for invoice in r.invoices:
                                if isinstance(invoice, str):
                                    logger.error(f"❌ Factura inválida (string): {invoice[:100]}...")
                                    continue
                                elif hasattr(invoice, '__dict__'):
                                    valid_invoices.append(invoice)
                                else:
                                    logger.error(f"❌ Factura de tipo inválido: {type(invoice)}")
                                    continue
                            
                            all_invoices.extend(valid_invoices)
                            logger.info(f"Cuenta {cfg.username}: {len(valid_invoices)} facturas válidas procesadas")
                        else:
                            errors.append(f"Error en {cfg.username}: {r.message}")
                            logger.error(f"Error en cuenta {cfg.username}: {r.message}")
                    else:
                        errors.append(f"Error en {cfg.username}: {result_data}")
                        logger.error(f"Error al procesar cuenta {cfg.username}: {result_data}")
                except queue.Empty:
                    errors.append(f"Error en {cfg.username}: no se pudo obtener resultado")
                    logger.error(f"Error al procesar cuenta {cfg.username}: no se pudo obtener resultado")
                    
            except Exception as e:
                errors.append(f"Error en {cfg.username}: {str(e)}")
                logger.error(f"Error al procesar cuenta {cfg.username}: {str(e)}")

        if all_invoices:
            unique = self._remove_duplicate_invoices(all_invoices)
            logger.info(f"Facturas únicas después de eliminar duplicados: {len(unique)} (originales: {len(all_invoices)})")

            # Persistir en MongoDB (cabecera + detalle)
            try:
                repo = MongoInvoiceRepository()
                docs = [map_invoice(inv, fuente="XML_NATIVO" if getattr(inv, 'cdc', '') else "OPENAI_VISION") for inv in unique]
                # Enriquecer con owner_email si está configurado (multi-tenant)
                if self.owner_email:
                    for d in docs:
                        try:
                            d.header.owner_email = self.owner_email
                            for it in d.items:
                                it.owner_email = self.owner_email
                        except Exception:
                            pass
                for d in docs:
                    repo.save_document(d)
                message_suffix = f" | MongoDB repo: {len(docs)} facturas almacenadas"
                logger.info(f"💾 MongoDB repo: {len(docs)} documentos (cabecera + detalle)")
            except Exception as e:
                logger.error(f"❌ Error persistiendo en MongoDB (repo): {e}")
                message_suffix = f" | ⚠️ Error MongoDB: {str(e)}"
            finally:
                try:
                    repo.close()
                except Exception:
                    pass
            all_invoices = unique

        if success_count == len(self.email_configs):
            message = f"Procesamiento exitoso de {len(self.email_configs)} cuentas. {len(all_invoices)} facturas encontradas."
        elif success_count > 0:
            message = f"Procesamiento parcial: {success_count}/{len(self.email_configs)} cuentas exitosas. {len(all_invoices)} facturas encontradas."
        else:
            message = f"Fallo en todas las cuentas. Errores: {'; '.join(errors)}"

        # Adjuntar información de export a MongoDB si está disponible
        try:
            if 'message_suffix' in locals() and message_suffix:
                message += message_suffix
        except Exception:
            pass

        # Excel deshabilitado

        return ProcessResult(
            success=success_count > 0,
            message=message,
            invoice_count=len(all_invoices),
            invoices=all_invoices
        )

    # ----------------------------
    # Scheduler LEGADO (schedule)
    # ----------------------------
    def start(self):
        if self._job_running:
            logger.warning("El scheduler ya está en ejecución")
            return
        interval = settings.JOB_INTERVAL_MINUTES
        logger.info(f"Iniciando scheduler cada {interval} minutos")
        schedule.every(interval).minutes.do(self._run_job)
        self._job_running = True
        self._job_thread = threading.Thread(target=self._loop, daemon=True)
        self._job_thread.start()

    def stop(self):
        if not self._job_running:
            logger.warning("El scheduler no está en ejecución")
            return
        logger.info("Deteniendo scheduler")
        self._job_running = False
        schedule.clear()
        if self._job_thread and self._job_thread.is_alive():
            self._job_thread.join(timeout=2)

    def _loop(self):
        while self._job_running:
            schedule.run_pending()
            time.sleep(1)

    def _run_job(self):
        logger.info("Ejecutando job programado para procesar múltiples correos")
        res = self.process_all_emails()
        (logger.info if res.success else logger.error)(res.message)
        return res

    # ------------------------------------------
    # NUEVO: API esperada por /job/start y /job/stop
    # ------------------------------------------
    def start_scheduled_job(self):
        """
        Mantiene el nombre que espera tu API.
        Inicia un runner que ejecuta process_all_emails() cada X minutos.
        """
        try:
            interval = settings.JOB_INTERVAL_MINUTES
        except Exception:
            interval = 60

        if self._scheduler and self._scheduler.is_running:
            # ya está corriendo; no lo dupliques
            logger.info("start_scheduled_job: ya en ejecución")
            return {"ok": True, "message": "El job ya está en ejecución."}

        self._scheduler = ScheduledJobRunner(
            interval_minutes=interval,
            target=self.process_all_emails
        )
        self._scheduler.start()
        logger.info(f"start_scheduled_job: iniciado (cada {interval} min)")
        return {"ok": True, "message": f"Job iniciado. Intervalo: {interval} minutos."}

    def stop_scheduled_job(self):
        """
        Detiene el job programado si está en ejecución.
        """
        if self._scheduler and self._scheduler.is_running:
            self._scheduler.stop()
            logger.info("stop_scheduled_job: detenido")
            return {"ok": True, "message": "Job detenido."}
        logger.info("stop_scheduled_job: no había job en ejecución")
        return {"ok": True, "message": "No había job en ejecución."}

    def scheduled_job_status(self):
        """
        Snapshot simple del runner moderno (opcional para /job/status).
        """
        if not self._scheduler or not self._scheduler.is_running:
            return {
                "running": False,
                "next_run": None,
                "last_run": None,
                "interval_minutes": settings.JOB_INTERVAL_MINUTES,
                "last_result": None
            }
        return {
            "running": True,
            "next_run": self._scheduler.next_run,
            "last_run": self._scheduler.last_run,
            "interval_minutes": settings.JOB_INTERVAL_MINUTES,
            "last_result": getattr(self._scheduler, "last_result", None)
        }

    def set_interval_minutes(self, minutes: int):
        try:
            minutes = max(1, int(minutes))
        except Exception:
            minutes = settings.JOB_INTERVAL_MINUTES
        settings.JOB_INTERVAL_MINUTES = minutes
        if self._scheduler and self._scheduler.is_running:
            # actualizar intervalo en caliente
            self._scheduler.interval_minutes = minutes
        return {"ok": True, "interval_minutes": minutes}


# =========================
#  EmailProcessor (single)
# =========================
class EmailProcessor:
    """
    Procesador para una sola cuenta.
    Separa responsabilidades: IMAP, parseo, guardado adjuntos, links y envío a OpenAI.
    """
    def __init__(self, config: EmailConfig = None):
        if config is None:
            # Obtener primera configuración habilitada desde MongoDB
            try:
                configs = get_enabled_configs(include_password=True)
            except Exception as e:
                logger.error(f"Error obteniendo configuraciones IMAP desde MongoDB: {e}")
                configs = []
            if configs:
                first = configs[0]
                self.config = EmailConfig(
                    host=first.get("host"),
                    port=int(first.get("port", 993)),
                    username=first.get("username"),
                    password=first.get("password", ""),
                    search_criteria=first.get("search_criteria", "UNSEEN"),
                    search_terms=first.get("search_terms") or []
                )
            else:
                raise ValueError("No hay configuraciones de correo habilitadas en la base de datos")
        else:
            self.config = config

        # Usar pool de conexiones en lugar de cliente directo
        self.connection_pool = get_imap_pool()
        self.current_connection = None
        
        # Mantener cliente legacy para compatibilidad (puede removerse después)
        self.client = IMAPClient(
            host=self.config.host, port=self.config.port,
            username=self.config.username, password=self.config.password, mailbox="INBOX"
        )
        self.openai_processor = OpenAIProcessor()
        # Estado para scheduler legacy
        self._last_run_iso: Optional[str] = None

        ensure_dirs()
        logger.info(f"✅ EmailProcessor inicializado con pool de conexiones para {self.config.username}")

    # --------- IMAP high-level con pool ---------
    def connect(self) -> bool:
        """Obtiene conexión del pool o crea una nueva."""
        if self.current_connection and self.current_connection.test_connection():
            return True
        
        self.current_connection = self.connection_pool.get_connection(self.config)
        if self.current_connection:
            logger.info(f"🔄 Conexión IMAP obtenida del pool para {self.config.username}")
            return True
        
        logger.error(f"❌ No se pudo obtener conexión IMAP para {self.config.username}")
        return False

    def disconnect(self):
        """Devuelve conexión al pool en lugar de cerrarla."""
        if self.current_connection:
            if self.connection_pool.return_connection(self.current_connection):
                logger.debug(f"↩️ Conexión devuelta al pool para {self.config.username}")
            else:
                logger.warning(f"⚠️ No se pudo devolver conexión al pool para {self.config.username}")
            self.current_connection = None

    def _get_imap_connection(self):
        """Obtiene la conexión IMAP actual."""
        if self.current_connection:
            return self.current_connection.connection
        return None

    # --------- Search logic ---------
    def search_emails(self) -> List[str]:
        """
        Usa IMAPClient.search(subject_terms) que devuelve UIDs (str).
        NOTA: los términos en .env deben venir SIN acentos (como acordamos).
        """
        if not self.client.conn:
            if not self.connect():
                return []

        terms = self.config.search_terms or []
        if not terms:
            logger.info("No se configuraron términos de búsqueda. Se devolverá lista vacía.")
            return []

        # Pasamos la lista de términos directamente al nuevo IMAPClient.search()
        unread_only = (str(self.config.search_criteria or 'UNSEEN').upper() != 'ALL')
        uids = self.client.search(terms, unread_only=unread_only)

        logger.info(f"Se encontraron {len(uids)} correos combinando términos: {terms}")
        return uids

    # --------- Fetch + parse ---------
    def get_email_content(self, email_id: str) -> Tuple[dict, list]:
        """
        Extrae subject/sender/date + adjuntos PDF/XML y links candidatos.
        """
        if not self.client.conn and not self.connect():
            return {}, []
        message = self.client.fetch_message(email_id)
        if not message:
            return {}, []

        subject = decode_mime_header(message.get("Subject", ""))
        sender = decode_mime_header(message.get("From", ""))
        date_str = message.get("Date", "")

        dt = None
        if date_str:
            import email as pyemail
            try:
                dt = email.utils.parsedate_to_datetime(date_str)
            except Exception as e:
                logger.warning(f"⚠️ Error al parsear fecha '{date_str}': {e}")

        meta = {"subject": subject, "sender": sender, "date": dt, "message_id": email_id}
        attachments = []
        links = extract_links_from_message(message)

        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            filename = part.get_filename()
            if not filename:
                continue
            filename = decode_mime_header(filename).strip()
            ctype = (part.get_content_type() or "").lower()
            content = part.get_payload(decode=True)

            is_pdf = filename.lower().endswith(".pdf") or ctype == "application/pdf"
            is_xml = filename.lower().endswith(".xml") or ctype in (
                "text/xml", "application/xml", "application/x-iso20022+xml", "application/x-invoice+xml"
            )
            if is_pdf or is_xml:
                logger.info(f"📎 Adjunto detectado: {filename} ({ctype})")
                attachments.append({
                    "filename": filename,
                    "content": content,
                    "content_type": ctype
                })

        meta["links"] = links
        logger.info(f"📬 Correo {email_id} - Asunto: '{subject}' - Adjuntos: {len(attachments)} - Enlaces: {len(links)}")
        return meta, attachments

    # --------- Core processing ---------
    def process_emails(self) -> ProcessResult:
        result = ProcessResult(success=True, message="Procesamiento completado", invoice_count=0, invoices=[])
        try:
            if not self.connect():
                return ProcessResult(success=False, message="Error al conectar al servidor de correo")

            email_ids = self.search_emails()
            if not email_ids:
                self.disconnect()
                return ProcessResult(success=True, message="No se encontraron correos con facturas", invoice_count=0)

            logger.info(f"Procesando {len(email_ids)} correos")

            abort_run = False

            for eid in email_ids:

                if abort_run:           
                    break

                try:
                    metadata, attachments = self.get_email_content(eid)
                    if not metadata:
                        logger.warning(f"⚠️ No se pudo obtener metadatos del correo {eid}")
                        continue

                    email_meta_for_ai = {
                        "sender": metadata.get("sender", ""),
                        "subject": metadata.get("subject", ""),
                        "date": metadata.get("date")
                    }

                    xml_path = None
                    pdf_path = None
                    processed = False

                    # Adjuntos: XML prioridad, luego PDF
                    for att in attachments:
                        fname = (att.get("filename") or "").lower()
                        ctype = (att.get("content_type") or "").lower()
                        content = att.get("content") or b""
                        is_pdf = fname.endswith(".pdf") or ctype == "application/pdf"
                        is_xml = fname.endswith(".xml") or ctype in (
                            "text/xml", "application/xml", "application/x-iso20022+xml", "application/x-invoice+xml"
                        )
                        if is_xml:
                            xml_path = save_binary(content, fname)  # no forzamos .pdf
                            logger.info(f"📄 XML adjunto detectado: {fname}")
                        elif is_pdf:
                            pdf_path = save_binary(content, fname, force_pdf=True)
                            logger.info(f"📄 PDF adjunto detectado: {fname}")
                    try:
                        # XML primero
                        if xml_path:
                            logger.info("📄 Procesando XML adjunto como fuente principal")
                            inv = self.openai_processor.extract_invoice_data_from_xml(xml_path, email_meta_for_ai)
                            if inv:
                                result.invoices.append(inv)
                                result.invoice_count += 1
                                processed = True

                        # Si el XML falló o no existe, intentar PDF
                        if not processed and pdf_path:
                            logger.info("📄 Procesando PDF como imagen (fallback o sin XML)")
                            inv = self.openai_processor.extract_invoice_data(pdf_path, email_meta_for_ai)
                            if inv:
                                result.invoices.append(inv)
                                result.invoice_count += 1
                                processed = True

                        # Enlaces si nada funcionó
                        if not processed and metadata.get("links"):
                            logger.info(f"🔗 Procesando {len(metadata['links'])} enlaces encontrados")
                            for link in metadata["links"]:
                                logger.info(f"🔗 Intentando procesar enlace: {link}")
                                downloaded_path = download_pdf_from_url(link)
                                if not downloaded_path:
                                    logger.warning(f"❌ No se pudo descargar desde el enlace: {link}")
                                    continue

                                low = downloaded_path.lower()
                                inv = None
                                if low.endswith(".xml"):
                                    logger.info("📄 XML detectado desde enlace, procesando como factura electrónica (SIFEN si aplica)")
                                    inv = self.openai_processor.extract_invoice_data_from_xml(downloaded_path)
                                elif low.endswith(".pdf"):
                                    logger.info("📄 PDF detectado desde enlace, procesando con OpenAI")
                                    inv = self.openai_processor.extract_invoice_data(downloaded_path, email_meta_for_ai)
                                else:
                                    logger.warning(f"⚠️ Tipo de archivo no reconocido: {downloaded_path}")
                                    continue

                                if inv:
                                    result.invoices.append(inv)
                                    result.invoice_count += 1
                                    processed = True
                    except OpenAIFatalError as e:         # ⬅️ NUEVO
                        logger.error(f"❌ Error FATAL de OpenAI en correo {eid}: {e}. Abortando lote.")
                        abort_run = True                   # ⬅️ NUEVO
                        processed = False                  # ⬅️ aseguramos que NO se marque leído
                        break                              # ⬅️ cortamos el loop completo

                    except OpenAIRetryableError as e:      # ⬅️ NUEVO
                        logger.warning(f"⚠️ Error transitorio de OpenAI en correo {eid}: {e}. Se omite este correo.")
                        processed = False                  # no marcar leído
                        continue
                        # Marcar leído si hubo procesamiento OK
                    if processed:
                        self.client.mark_seen(eid)
                        # Limpieza de temporales asociados a este correo
                        try:
                            import os
                            if xml_path and os.path.exists(xml_path):
                                os.remove(xml_path)
                            if pdf_path and os.path.exists(pdf_path):
                                os.remove(pdf_path)
                        except Exception:
                            pass
                    else:
                        logger.warning(f"⚠️ Ninguna factura procesada del correo {eid}, no se marcará como leído.")

                except Exception as e:
                    logger.error(f"❌ Error al procesar el correo {eid}: {str(e)}")
                    continue
            
            # Si abortamos por fatal, devolvemos estado de error
            if abort_run:
                self.disconnect()
                return ProcessResult(
                    success=False,
                    message="Procesamiento abortado por error fatal de OpenAI (API key/cuota).",
                    invoice_count=len(result.invoices),
                    invoices=result.invoices
            )

            # Persistir en MongoDB (automatizado y manual comparten esta ruta)
            if result.invoices:
                try:
                    repo = MongoInvoiceRepository()
                    docs = [map_invoice(inv, fuente="XML_NATIVO" if getattr(inv, 'cdc', '') else "OPENAI_VISION") for inv in result.invoices]
                    for d in docs:
                        repo.save_document(d)
                    logger.info(f"💾 MongoDB (repo): {len(docs)} facturas almacenadas")
                    result.message = f"Se procesaron {result.invoice_count} facturas. Persistidas en MongoDB (cabecera + detalle)"
                except Exception as e:
                    logger.error(f"❌ Error persistiendo en MongoDB (repo): {e}")
                    result.message = f"Se procesaron {result.invoice_count} facturas, pero falló la persistencia en MongoDB"
                finally:
                    try:
                        repo.close()
                    except Exception:
                        pass

            self.disconnect()
            return result

        except Exception as e:
            logger.error(f"Error general en el procesamiento: {str(e)}")
            self.disconnect()
            return ProcessResult(success=False, message=f"Error en el procesamiento: {str(e)}")

    # ------------- (Opcional) scheduler single -------------
    def start_scheduled_job(self):
        """
        Conservamos esta API por compatibilidad, pero recomendamos usar MultiEmailProcessor.start_scheduled_job().
        """
        if getattr(self, "_job_running", False):
            logger.warning("El job ya está en ejecución")
            return
        interval = settings.JOB_INTERVAL_MINUTES
        logger.info(f"Iniciando job programado para ejecutarse cada {interval} minutos")
        schedule.every(interval).minutes.do(self._run_job)
        self._interval_minutes = interval
        self._job_running = True
        self._job_thread = threading.Thread(target=self._schedule_loop, daemon=True)
        self._job_thread.start()

    def stop_scheduled_job(self):
        if not getattr(self, "_job_running", False):
            logger.warning("El job no está en ejecución")
            return
        logger.info("Deteniendo job programado")
        self._job_running = False
        schedule.clear()
        if getattr(self, "_job_thread", None) and self._job_thread.is_alive():
            self._job_thread.join(timeout=2)

    def _schedule_loop(self):
        while getattr(self, "_job_running", False):
            schedule.run_pending()
            time.sleep(1)

    def _run_job(self):
        logger.info("Ejecutando job programado para procesar correos")
        try:
            from datetime import datetime
            self._last_run_iso = datetime.now().isoformat()
        except Exception:
            self._last_run_iso = None
        res = self.process_emails()
        (logger.info if res.success else logger.error)(res.message)
        return res

    # Permitir ajustar el intervalo para el scheduler basado en 'schedule'
    def set_interval_minutes(self, minutes: int):
        from app.config.settings import settings as _settings
        try:
            minutes = max(1, int(minutes))
        except Exception:
            minutes = _settings.JOB_INTERVAL_MINUTES
        _settings.JOB_INTERVAL_MINUTES = minutes
        if getattr(self, "_job_running", False):
            try:
                import schedule
                schedule.clear()
            except Exception:
                pass
            # reiniciar con nuevo intervalo
            logger.info(f"Reiniciando job con nuevo intervalo: {minutes} min")
            schedule.every(minutes).minutes.do(self._run_job)
        return {"ok": True, "interval_minutes": minutes}

    def scheduled_job_status(self):
        """Snapshot del scheduler legacy (schedule)."""
        try:
            import schedule
            next_run_iso = None
            if getattr(self, "_job_running", False) and getattr(schedule, "next_run", None):
                try:
                    # schedule.next_run es una función en esta versión;
                    nr = schedule.next_run() if callable(getattr(schedule, 'next_run', None)) else getattr(schedule, 'next_run', None)
                    if nr is not None:
                        next_run_iso = getattr(nr, 'isoformat', lambda: str(nr))()
                except Exception:
                    try:
                        next_run_iso = str(schedule.next_run())
                    except Exception:
                        next_run_iso = None
            return {
                "running": bool(getattr(self, "_job_running", False)),
                "next_run": next_run_iso,
                "last_run": self._last_run_iso,
                "interval_minutes": getattr(self, "_interval_minutes", settings.JOB_INTERVAL_MINUTES),
                "last_result": None
            }
        except Exception:
            return {
                "running": bool(getattr(self, "_job_running", False)),
                "next_run": None,
                "last_run": self._last_run_iso,
                "interval_minutes": getattr(self, "_interval_minutes", settings.JOB_INTERVAL_MINUTES),
                "last_result": None
            }
    
