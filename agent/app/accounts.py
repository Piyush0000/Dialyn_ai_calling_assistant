"""Store accounts: signup, login sessions, team members and API keys.

Both kinds of credential are sent as ``Authorization: Bearer <token>``:
  sk_live_...  API key   -> server-to-server integrations
  sess_...     session   -> dashboard users (from /auth/login or /auth/signup)

  POST /auth/signup                  New store + owner account (if ALLOW_SIGNUP)
  POST /auth/login, POST /auth/logout, GET /auth/me
  POST /auth/accept-invite           Join a store from an invite link
  GET  /v1/team                      Members and pending invites
  POST /v1/team/invites              Invite a teammate (owner/admin) -> invite link
  DELETE /v1/team/members/{id}       Remove a teammate (owner)
  GET/POST /v1/api-keys, DELETE /v1/api-keys/{id}   Manage API keys (owner/admin)
"""

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, EmailStr, Field

from app.context import deps, settings
from app.db import Tenant, User, now_utc

router = APIRouter()

SESSION_TTL = timedelta(days=30)
INVITE_TTL = timedelta(days=7)
MAX_LOGIN_FAILURES = 5
LOCKOUT_SECS = 15 * 60
_login_failures: dict[str, tuple[int, float]] = {}


# ---------------------------------------------------------------- secrets


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_api_key() -> str:
    return "sk_live_" + secrets.token_urlsafe(32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt_b64), n=2**14, r=8, p=1, dklen=32
        )
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError):
        return False


async def provision_tenant(**fields) -> tuple[Tenant, str, str]:
    """Create a store with its first API key. Returns (tenant, api_key, webhook_secret)."""
    api_key = new_api_key()
    webhook_secret = "whsec_" + secrets.token_urlsafe(24)
    tenant = await deps.store.create_tenant(
        **fields,
        # Legacy columns (unique); real keys live in api_keys.
        api_key_hash=sha256(secrets.token_hex(32)),
        api_key_prefix="",
        webhook_secret=webhook_secret,
    )
    await deps.store.create_api_key(tenant.id, sha256(api_key), api_key[:12], "Default")
    return tenant, api_key, webhook_secret


async def _start_session(user: User) -> str:
    token = "sess_" + secrets.token_urlsafe(32)
    await deps.store.create_session(
        token_hash=sha256(token),
        user_id=user.id,
        tenant_id=user.tenant_id,
        expires_at=now_utc() + SESSION_TTL,
    )
    await deps.store.update_user(user.id, last_login_at=now_utc())
    return token


# ------------------------------------------------------------------- auth


@dataclass
class Principal:
    tenant: Tenant
    user: User | None = None  # None when authenticated with an API key
    token: str = ""


async def current_principal(authorization: str = Header(default="")) -> Principal:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Use 'Authorization: Bearer <api key>'")
    if token.startswith("sess_"):
        login = await deps.store.get_session(sha256(token))
        user = await deps.store.get_user(login.user_id) if login else None
        tenant = await deps.store.get_tenant(login.tenant_id) if login else None
        if user is None or tenant is None:
            raise HTTPException(status_code=401, detail="Session expired; sign in again")
        return Principal(tenant=tenant, user=user, token=token)
    tenant = await deps.store.get_tenant_by_key_hash(sha256(token))
    if tenant is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return Principal(tenant=tenant, token=token)


async def current_tenant(principal: Principal = Depends(current_principal)) -> Tenant:
    return principal.tenant


def require_role(*roles: str):
    async def check(principal: Principal = Depends(current_principal)) -> Principal:
        if principal.user is None:
            raise HTTPException(status_code=403, detail="Sign in to the dashboard to do this")
        if principal.user.role not in roles:
            raise HTTPException(status_code=403, detail="Your role cannot do this")
        return principal

    return check


class SignupBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    brand_name: str = Field(min_length=1, max_length=120)


class LoginBody(BaseModel):
    email: EmailStr
    password: str


class AcceptInviteBody(BaseModel):
    token: str
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=200)


def _me(user: User, tenant: Tenant) -> dict:
    return {"user": user.to_dict(), "tenant": tenant.to_dict()}


@router.post("/auth/signup", status_code=201)
async def signup(body: SignupBody):
    if not settings.allow_signup:
        raise HTTPException(status_code=403, detail="Signups are closed on this server")
    email = body.email.lower()
    if await deps.store.get_user_by_email(email):
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    tenant, api_key, webhook_secret = await provision_tenant(
        name=body.brand_name, brand_name=body.brand_name
    )
    user = await deps.store.create_user(
        tenant_id=tenant.id,
        email=email,
        name=body.name,
        password_hash=hash_password(body.password),
        role="owner",
    )
    return {
        "session_token": await _start_session(user),
        "api_key": api_key,
        "webhook_secret": webhook_secret,
        **_me(user, tenant),
    }


@router.post("/auth/login")
async def login(body: LoginBody):
    email = body.email.lower()
    failures, locked_until = _login_failures.get(email, (0, 0.0))
    if locked_until > time.time():
        raise HTTPException(status_code=429, detail="Too many attempts; try again in 15 minutes")
    user = await deps.store.get_user_by_email(email)
    if user is None or not verify_password(body.password, user.password_hash):
        failures += 1
        _login_failures[email] = (
            failures,
            time.time() + LOCKOUT_SECS if failures >= MAX_LOGIN_FAILURES else 0.0,
        )
        raise HTTPException(status_code=401, detail="Wrong email or password")
    _login_failures.pop(email, None)
    tenant = await deps.store.get_tenant(user.tenant_id)
    return {"session_token": await _start_session(user), **_me(user, tenant)}


@router.post("/auth/logout")
async def logout(principal: Principal = Depends(current_principal)):
    if principal.user:
        await deps.store.delete_session(sha256(principal.token))
    return {"ok": True}


@router.get("/auth/me")
async def me(principal: Principal = Depends(current_principal)):
    if principal.user is None:
        return {"user": None, "tenant": principal.tenant.to_dict()}
    return _me(principal.user, principal.tenant)


@router.post("/auth/accept-invite", status_code=201)
async def accept_invite(body: AcceptInviteBody):
    invite = await deps.store.get_invite(sha256(body.token))
    if invite is None:
        raise HTTPException(status_code=404, detail="This invite link is invalid or has expired")
    if await deps.store.get_user_by_email(invite.email):
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    user = await deps.store.create_user(
        tenant_id=invite.tenant_id,
        email=invite.email,
        name=body.name,
        password_hash=hash_password(body.password),
        role=invite.role,
    )
    await deps.store.delete_invite(invite.token_hash)
    tenant = await deps.store.get_tenant(user.tenant_id)
    return {"session_token": await _start_session(user), **_me(user, tenant)}


# ------------------------------------------------------------------- team


class InviteBody(BaseModel):
    email: EmailStr
    role: Literal["admin", "member"] = "member"


@router.get("/v1/team")
async def team(principal: Principal = Depends(require_role("owner", "admin", "member"))):
    users = await deps.store.list_users(principal.tenant.id)
    invites = await deps.store.list_invites(principal.tenant.id)
    return {
        "members": [u.to_dict() for u in users],
        "invites": [
            {"email": i.email, "role": i.role, "expires_at": i.expires_at} for i in invites
        ],
        "you": principal.user.id,
    }


@router.post("/v1/team/invites", status_code=201)
async def invite(body: InviteBody, principal: Principal = Depends(require_role("owner", "admin"))):
    email = body.email.lower()
    if await deps.store.get_user_by_email(email):
        raise HTTPException(status_code=409, detail="This person already has an account")
    token = "inv_" + secrets.token_urlsafe(24)
    await deps.store.create_invite(
        token_hash=sha256(token),
        tenant_id=principal.tenant.id,
        email=email,
        role=body.role,
        invited_by=principal.user.id,
        expires_at=now_utc() + INVITE_TTL,
    )
    return {
        "invite_path": f"/dashboard#/join/{token}",
        "note": "Send this link to your teammate; it works once and expires in 7 days.",
    }


@router.delete("/v1/team/members/{user_id}")
async def remove_member(user_id: str, principal: Principal = Depends(require_role("owner"))):
    user = await deps.store.get_user(user_id)
    if user is None or user.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="Member not found")
    if user.id == principal.user.id:
        raise HTTPException(status_code=409, detail="You cannot remove yourself")
    await deps.store.delete_user(user.id)
    return {"ok": True}


# --------------------------------------------------------------- API keys


class NewKeyBody(BaseModel):
    name: str = Field(default="Default", min_length=1, max_length=80)


@router.get("/v1/api-keys")
async def list_keys(principal: Principal = Depends(require_role("owner", "admin"))):
    return {"data": [k.to_dict() for k in await deps.store.list_api_keys(principal.tenant.id)]}


@router.post("/v1/api-keys", status_code=201)
async def create_key(
    body: NewKeyBody, principal: Principal = Depends(require_role("owner", "admin"))
):
    api_key = new_api_key()
    key = await deps.store.create_api_key(
        principal.tenant.id, sha256(api_key), api_key[:12], body.name
    )
    return {**key.to_dict(), "api_key": api_key, "note": "Copy it now; it is not shown again."}


@router.delete("/v1/api-keys/{key_id}")
async def revoke_key(key_id: str, principal: Principal = Depends(require_role("owner", "admin"))):
    if not await deps.store.revoke_api_key(principal.tenant.id, key_id):
        raise HTTPException(status_code=404, detail="API key not found")
    return {"ok": True}
