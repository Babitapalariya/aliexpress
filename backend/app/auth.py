from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import AliExpressToken

from iop.base import IopClient, IopRequest
import logging

router = APIRouter(tags=["auth"])
settings = get_settings()

logger = logging.getLogger(__name__)


class AuthorizationCode(BaseModel):
    code: str


@router.post("/aliexpress/authorize")
def authorize_aliexpress(payload: AuthorizationCode, db: Session = Depends(get_db)):
    code = payload.code.strip()
    if not code or len(code) > 4096:
        raise HTTPException(400, "Enter a valid AliExpress authorization code.")
    return callback(code, db)


@router.post("/shopify/reconnect")
def reconnect_shopify():
    from .shopify import _get_shopify_token

    _get_shopify_token(force_refresh=True)
    return {"message": "Shopify reconnected successfully"}


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

    except Exception as exc:
        raise HTTPException(502, "AliExpress authorization request failed. Please try again.") from exc

    if not isinstance(data, dict) or not data.get("access_token"):
        raise HTTPException(400, "AliExpress rejected the authorization code. Get a new code using Reconnect AliExpress and try again.")

    saved = save_token(db, data)
    return {
        "message": "Token saved successfully",
        "token_id": saved.id,
        "expires_in": saved.expires_in,
        "created_at": saved.created_at,
        "updated_at": saved.updated_at,
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

    try:
        client = IopClient(
            settings.ALIEXPRESS_API_URL,
            settings.ALIEXPRESS_APP_KEY,
            settings.ALIEXPRESS_APP_SECRET,
        )
        request = IopRequest("/auth/token/refresh", "POST")
        request.add_api_param("app_key", settings.ALIEXPRESS_APP_KEY)
        request.add_api_param("refresh_token", token.refresh_token)
        data = client.execute(request).body
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not complete the AliExpress refresh request. Please try again.",
        ) from exc

    if not isinstance(data, dict):
        raise HTTPException(502, "AliExpress returned an invalid refresh response.")
    if not data.get("access_token"):
        error = data.get("error_response") or data
        if not isinstance(error, dict):
            error = {}
        reason = str(error.get("message") or error.get("msg") or "No access token returned")
        # Never expose stored credentials, even if the provider echoes them.
        for secret in (token.refresh_token, token.access_token, settings.ALIEXPRESS_APP_SECRET):
            if secret:
                reason = reason.replace(secret, "[redacted]")
        raise HTTPException(
            400,
            detail=f"AliExpress rejected token refresh: {reason}. If authorization has expired, use Reconnect AliExpress.",
        )

    saved = save_token(db, data)
    return {
        "message": "AliExpress token refreshed",
        "token_id": saved.id,
        "expires_in": saved.expires_in,
    }
