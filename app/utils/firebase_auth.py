from typing import Optional, Dict, Any
from fastapi import HTTPException, Request
from app.config.settings import settings
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests


def verify_firebase_token(token: str) -> Dict[str, Any]:
    try:
        req = google_requests.Request()
        claims = id_token.verify_firebase_token(token, req, audience=settings.FIREBASE_PROJECT_ID)
        if not claims:
            raise ValueError("Token inválido")
        # Validar issuer por seguridad
        iss = claims.get('iss') or ''
        expected_iss = f"https://securetoken.google.com/{settings.FIREBASE_PROJECT_ID}"
        if expected_iss not in iss:
            raise ValueError("Issuer inválido")
        return claims
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Auth inválida: {str(e)}")


def extract_bearer_token(request: Request) -> Optional[str]:
    auth = request.headers.get('Authorization') or ''
    if not auth.lower().startswith('bearer '):
        return None
    return auth.split(' ', 1)[1].strip()

