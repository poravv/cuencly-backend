import os
import re
import time
import uuid
import hashlib
import logging
from datetime import datetime
from typing import Tuple

from app.config.settings import settings

logger = logging.getLogger(__name__)

_FALLBACK_DIR = "/tmp/cuenlyapp/temp_pdfs"

def _ensure_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        # Verificar escritura
        test_path = os.path.join(path, ".write_test")
        with open(test_path, "w") as f:
            f.write("ok")
        os.remove(test_path)
        return True
    except Exception as e:
        logger.error(f"❌ No se pudo crear/escribir en {path}: {e}")
        return False

def ensure_dirs():
    """Garantiza que exista un directorio usable para temporales.
    Intenta settings.TEMP_PDF_DIR y cae a /tmp si falla.
    """
    if not _ensure_dir(settings.TEMP_PDF_DIR):
        logger.warning(f"⚠️ Usando directorio fallback para temporales: {_FALLBACK_DIR}")
        _ensure_dir(_FALLBACK_DIR)

def sanitize_filename(filename: str, force_pdf: bool = False) -> str:
    """Limpia el nombre y fuerza .pdf si se requiere."""
    safe = re.sub(r'[<>:"/\\|?*]', '_', filename or "")
    safe = re.sub(r'[\x00-\x1f\x7f-\x9f]', '_', safe)
    safe = re.sub(r'\s+', '_', safe.strip())
    name, ext = os.path.splitext(safe)
    if len(name) > 100:
        name = name[:100]
    if force_pdf and not ext.lower().endswith(".pdf"):
        ext = ".pdf"
    return f"{name}{ext or ''}"

def unique_name(clean_name: str) -> str:
    """timestamp + uuid + base."""
    ts = datetime.now().strftime("%Y%m%d%H%M%S%f")[:-3]
    uid = uuid.uuid4().hex[:8]
    name, ext = os.path.splitext(clean_name)
    return f"{ts}_{uid}_{name}{ext}"

def _resolve_base_dir() -> str:
    # Intentar usar el configurado; si no se puede escribir, usar fallback
    if _ensure_dir(settings.TEMP_PDF_DIR):
        return settings.TEMP_PDF_DIR
    _ensure_dir(_FALLBACK_DIR)
    return _FALLBACK_DIR

def save_binary(content: bytes, filename: str, force_pdf: bool = False) -> str:
    """Guarda bytes en /temp_pdfs con nombre único."""
    try:
        ensure_dirs()
        clean = sanitize_filename(filename, force_pdf=force_pdf)
        candidate = unique_name(clean)
        base_dir = _resolve_base_dir()
        path = os.path.join(base_dir, candidate)
        with open(path, "wb") as f:
            f.write(content)
        logger.info(f"🗂 Archivo guardado: {path}")
        return path
    except Exception as e:
        logger.error(f"❌ Error al guardar archivo {filename}: {e}")
        return ""

def cleanup_temp_dir(older_than_hours: int = 24) -> int:
    """Elimina archivos en el dir temporal más viejo que X horas."""
    import time
    base_dir = _resolve_base_dir()
    cutoff = time.time() - older_than_hours * 3600
    removed = 0
    try:
        for name in os.listdir(base_dir):
            p = os.path.join(base_dir, name)
            try:
                if not os.path.isfile(p):
                    continue
                st = os.stat(p)
                if st.st_mtime < cutoff:
                    os.remove(p)
                    removed += 1
            except Exception:
                continue
    except Exception:
        pass
    return removed

def filename_from_url(url: str, extension: str) -> str:
    """Intenta construir nombre informativo desde la URL; fallback a dominio+hash."""
    ts = int(time.time())
    from urllib.parse import urlparse, parse_qs
    try:
        p = urlparse(url)
        qs = parse_qs(p.query)
        ruc = _first_contains(qs, "ruc")
        cdc = _first_contains_any(qs, ["cdc", "codigo", "code", "document", "doc"])
        num = _first_contains_any(qs, ["factura", "invoice", "numero", "number", "num"])

        parts = []
        if ruc: parts.append(f"ruc_{_clean_id(ruc)}")
        if cdc: parts.append(f"cdc_{_clean_id(cdc)[:12]}")
        if num: parts.append(f"num_{_clean_id(num)[:10]}")

        if parts:
            return f"factura_{'_'.join(parts)}_{ts}.{extension}"
    except Exception as e:
        logger.warning(f"Error parseando URL para nombre: {e}")

    try:
        p = urlparse(url)
        domain = (p.netloc or "unknown").replace(".", "_").replace(":", "_")[:20]
        domain = re.sub(r'[^\w\-_]', '', domain)
    except:
        domain = "unknown"
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    return f"factura_{domain}_{url_hash}_{ts}.{extension}"

def _first_contains(qs: dict, key: str) -> str:
    for k, v in qs.items():
        if key in k.lower() and v:
            return v[0]
    return ""

def _first_contains_any(qs: dict, keys) -> str:
    for k, v in qs.items():
        lk = k.lower()
        if any(kk in lk for kk in keys) and v:
            return v[0]
    return ""

def _clean_id(s: str) -> str:
    return re.sub(r"[^\w\-]", "", s or "")
