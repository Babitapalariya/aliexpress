from urllib.parse import urlencode

import requests
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import AliExpressToken

from iop.base import IopClient, IopRequest
import logging

router = APIRouter(tags=["auth"])
settings = get_settings()

logger = logging.getLogger(__name__)


def save_token(db: Session, data: dict) -> AliExpressToken:
    """Upsert the latest AliExpress token into the database."""
    access_token = data.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=400,
            detail={"message": "AliExpress did not return an access token", "response": data},
        )

    token = db.query(AliExpressToken).order_by(AliExpressToken.id.desc()).first()
    if token is None:
        token = AliExpressToken()
        db.add(token)

    token.access_token = access_token
    token.refresh_token = data.get("refresh_token") or getattr(token, "refresh_token", None)
    token.expires_in = data.get("expires_in")
    db.commit()
    db.refresh(token)
    return token


def get_latest_token(db: Session) -> AliExpressToken:
    """Retrieve the most recently saved token, raising 401 if none exists."""
    token = db.query(AliExpressToken).order_by(AliExpressToken.id.desc()).first()
    if token is None or not token.access_token:
        raise HTTPException(
            status_code=401,
            detail="No access token found in database. Please visit /login and complete OAuth.",
        )
    return token


# ──────────────────────────────────────────────
# STEP 1 — Return the auth URL as JSON (no redirect)
# ──────────────────────────────────────────────
@router.get("/login")
def login():
    """Return the AliExpress OAuth URL so the client can open it."""
    query = urlencode(
        {
            "response_type": "code",
            "force_auth": "true",
            "redirect_uri": settings.REDIRECT_URI,
            "client_id": settings.ALIEXPRESS_APP_KEY,
        }
    )
    auth_url = f"https://api-sg.aliexpress.com/oauth/authorize?{query}"
    return {"auth_url": auth_url}


# ──────────────────────────────────────────────
# STEP 2 — Exchange code for token and save to DB
# ──────────────────────────────────────────────
@router.get("/callback")
def callback(code: str, db: Session = Depends(get_db)):
    """
    AliExpress redirects here with ?code=...
    Exchange the code for an access_token and persist it to the database.
    """
    try:
        client = IopClient(
            settings.ALIEXPRESS_API_URL,
            settings.ALIEXPRESS_APP_KEY,
            settings.ALIEXPRESS_APP_SECRET,
        )

        request = IopRequest("/auth/token/create", "POST")
        request.add_api_param("app_key", settings.ALIEXPRESS_APP_KEY)
        request.add_api_param("code", code)

        response = client.execute(request)
        data = response.body

        logger.info("AliExpress token response: %s", data)

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    saved = save_token(db, data)
    return {
        "message": "Token saved successfully",
        "token_id": saved.id,
        "access_token": saved.access_token,
        "refresh_token": saved.refresh_token,
        "expires_in": saved.expires_in,
        "created_at": saved.created_at,
        "updated_at": saved.updated_at,
        # Raw AliExpress response (contains extra fields like user_id, expire_time, etc.)
        "aliexpress_raw_response": data,
    }


# ──────────────────────────────────────────────
# STEP 3 — Refresh token
# ──────────────────────────────────────────────
@router.post("/refresh-token")
def refresh_token(db: Session = Depends(get_db)):
    """Use the stored refresh_token to obtain a new access_token."""
    token = db.query(AliExpressToken).order_by(AliExpressToken.id.desc()).first()
    if token is None or not token.refresh_token:
        raise HTTPException(
            status_code=404,
            detail="No refresh token found. Visit /login first.",
        )

    payload = {
        "grant_type": "refresh_token",
        "client_id": settings.ALIEXPRESS_APP_KEY,
        "client_secret": settings.ALIEXPRESS_APP_SECRET,
        "refresh_token": token.refresh_token,
    }

    try:
        response = requests.post(
            "https://oauth.aliexpress.com/token", data=payload, timeout=30
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502, detail=f"AliExpress refresh request failed: {exc}"
        ) from exc

    saved = save_token(db, response.json())
    return {
        "message": "AliExpress token refreshed",
        "token_id": saved.id,
        "expires_in": saved.expires_in,
    }