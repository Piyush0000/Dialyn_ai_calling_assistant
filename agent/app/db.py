"""Persistence: merchants (tenants) and their calls. SQLite in dev, Postgres in prod."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    Integer,
    String,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now_utc() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    # SQLite drops tzinfo; everything we store is UTC.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    """A merchant / e-commerce platform using the API."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(120))
    # Legacy single-key columns; keys now live in ``api_keys`` (migrated on startup).
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    api_key_prefix: Mapped[str] = mapped_column(String(16))
    brand_name: Mapped[str] = mapped_column(String(120))
    agent_name: Mapped[str] = mapped_column(String(40), default="Priya")
    default_language: Mapped[str] = mapped_column(String(8), default="en")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    call_window_start: Mapped[str] = mapped_column(String(5), default="09:00")
    call_window_end: Mapped[str] = mapped_column(String(5), default="21:00")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    retry_delay_minutes: Mapped[int] = mapped_column(Integer, default=30)
    max_concurrent_calls: Mapped[int] = mapped_column(Integer, default=5)
    from_number: Mapped[str | None] = mapped_column(String(32))
    support_number: Mapped[str | None] = mapped_column(String(32))
    webhook_url: Mapped[str | None] = mapped_column(String(512))
    webhook_secret: Mapped[str] = mapped_column(String(64))
    # Per-language provider overrides, e.g. {"hi": {"tts": {"provider": "sarvam", ...}}}
    voice: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # twilio | plivo | exotel
    telephony_provider: Mapped[str | None] = mapped_column(String(16))
    # {"shopify": {"secret": ..., "events": {...}, ...}, "woocommerce": {...}}
    integrations: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Recorded human greetings: {"<lang>:<event>": {"path": ..., "text": ..., "secs": ...}}
    clips: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    def to_dict(self) -> dict[str, Any]:
        data = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        for private in ("api_key_hash", "api_key_prefix", "webhook_secret", "integrations"):
            data.pop(private)
        data["voice"] = data["voice"] or {}
        data["clips"] = {
            key: {"text": clip.get("text"), "secs": clip.get("secs")}
            for key, clip in (data["clips"] or {}).items()
        }
        return data


class User(Base):
    """A person who signs in to a store's dashboard."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="member")  # owner | admin | member
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "role": self.role,
            "created_at": _as_utc(self.created_at),
            "last_login_at": _as_utc(self.last_login_at),
        }


class LoginSession(Base):
    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Invite(Base):
    __tablename__ = "invites"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    email: Mapped[str] = mapped_column(String(254))
    role: Mapped[str] = mapped_column(String(16), default="member")
    invited_by: Mapped[str] = mapped_column(String(36))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(80), default="Default")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "prefix": self.prefix,
            "created_at": _as_utc(self.created_at),
            "last_used_at": _as_utc(self.last_used_at),
        }


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str | None] = mapped_column(String(36), index=True)
    agent_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str | None] = mapped_column(String(40), index=True)
    channel: Mapped[str] = mapped_column(String(8), default="phone")  # phone | web
    direction: Mapped[str] = mapped_column(String(16))  # inbound | outbound | web
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    provider: Mapped[str] = mapped_column(String(16), default="twilio")
    provider_call_id: Mapped[str | None] = mapped_column(String(64), index=True)
    from_number: Mapped[str | None] = mapped_column(String(32))
    to_number: Mapped[str | None] = mapped_column(String(32))
    customer_name: Mapped[str | None] = mapped_column(String(120))
    language: Mapped[str | None] = mapped_column(String(8))
    order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), index=True)
    variables: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    outcome: Mapped[str | None] = mapped_column(String(40), index=True)
    outcome_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    transcript: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    timeline: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    end_reason: Mapped[str | None] = mapped_column(String(64))
    recording_path: Mapped[str | None] = mapped_column(String(512))
    duration_secs: Mapped[float | None] = mapped_column(Float)
    webhook_status: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def to_dict(self) -> dict[str, Any]:
        data = {}
        for column in self.__table__.columns:
            key = "metadata_" if column.name == "metadata" else column.name
            value = getattr(self, key)
            data[column.name] = _as_utc(value) if isinstance(value, datetime) else value
        data["has_recording"] = bool(data.pop("recording_path"))
        return data


ACTIVE_STATUSES = ("dialing", "ringing", "in_progress")


class CallStore:
    def __init__(self, database_url: str):
        self.engine: AsyncEngine = create_async_engine(database_url)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_add_missing_columns)
        await self._migrate_legacy_api_keys()

    async def _migrate_legacy_api_keys(self) -> None:
        """Tenants created before multi-key support keep their original key working."""
        async with self._sessions() as session:
            migrated = select(ApiKey.tenant_id)
            legacy = await session.execute(select(Tenant).where(Tenant.id.not_in(migrated)))
            for tenant in legacy.scalars():
                if tenant.api_key_prefix.startswith("sk_live_"):
                    session.add(
                        ApiKey(
                            tenant_id=tenant.id,
                            key_hash=tenant.api_key_hash,
                            prefix=tenant.api_key_prefix,
                            name="Default",
                        )
                    )
            await session.commit()

    async def close(self) -> None:
        await self.engine.dispose()

    # ------------------------------------------------------------- tenants

    async def create_tenant(self, **fields: Any) -> Tenant:
        async with self._sessions() as session:
            tenant = Tenant(**fields)
            session.add(tenant)
            await session.commit()
            return tenant

    async def get_tenant(self, tenant_id: str) -> Tenant | None:
        async with self._sessions() as session:
            return await session.get(Tenant, tenant_id)

    async def get_tenant_by_key_hash(self, key_hash: str) -> Tenant | None:
        """Resolve an active API key to its tenant (and note when it was last used)."""
        async with self._sessions() as session:
            key = (
                (
                    await session.execute(
                        select(ApiKey).where(
                            ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None)
                        )
                    )
                )
                .scalars()
                .first()
            )
            if key is None:
                return None
            key.last_used_at = now_utc()
            await session.commit()
            return await session.get(Tenant, key.tenant_id)

    # ---------------------------------------------------------- API keys

    async def create_api_key(self, tenant_id: str, key_hash: str, prefix: str, name: str) -> ApiKey:
        async with self._sessions() as session:
            key = ApiKey(tenant_id=tenant_id, key_hash=key_hash, prefix=prefix, name=name)
            session.add(key)
            await session.commit()
            return key

    async def list_api_keys(self, tenant_id: str) -> list[ApiKey]:
        async with self._sessions() as session:
            result = await session.execute(
                select(ApiKey)
                .where(ApiKey.tenant_id == tenant_id, ApiKey.revoked_at.is_(None))
                .order_by(ApiKey.created_at)
            )
            return list(result.scalars())

    async def revoke_api_key(self, tenant_id: str, key_id: str) -> bool:
        async with self._sessions() as session:
            key = await session.get(ApiKey, key_id)
            if key is None or key.tenant_id != tenant_id or key.revoked_at is not None:
                return False
            key.revoked_at = now_utc()
            await session.commit()
            return True

    # ------------------------------------------------------ users/sessions

    async def create_user(self, **fields: Any) -> User:
        async with self._sessions() as session:
            user = User(**fields)
            session.add(user)
            await session.commit()
            return user

    async def get_user(self, user_id: str) -> User | None:
        async with self._sessions() as session:
            return await session.get(User, user_id)

    async def get_user_by_email(self, email: str) -> User | None:
        async with self._sessions() as session:
            result = await session.execute(select(User).where(User.email == email))
            return result.scalars().first()

    async def list_users(self, tenant_id: str) -> list[User]:
        async with self._sessions() as session:
            result = await session.execute(
                select(User).where(User.tenant_id == tenant_id).order_by(User.created_at)
            )
            return list(result.scalars())

    async def update_user(self, user_id: str, **fields: Any) -> None:
        async with self._sessions() as session:
            user = await session.get(User, user_id)
            if user is not None:
                for key, value in fields.items():
                    setattr(user, key, value)
                await session.commit()

    async def delete_user(self, user_id: str) -> None:
        async with self._sessions() as session:
            user = await session.get(User, user_id)
            if user is not None:
                await session.delete(user)
            logins = await session.execute(
                select(LoginSession).where(LoginSession.user_id == user_id)
            )
            for login in logins.scalars():
                await session.delete(login)
            await session.commit()

    async def create_session(self, **fields: Any) -> None:
        async with self._sessions() as session:
            session.add(LoginSession(**fields))
            await session.commit()

    async def get_session(self, token_hash: str) -> LoginSession | None:
        async with self._sessions() as session:
            login = await session.get(LoginSession, token_hash)
            if login is None or _as_utc(login.expires_at) < now_utc():
                return None
            return login

    async def delete_session(self, token_hash: str) -> None:
        async with self._sessions() as session:
            login = await session.get(LoginSession, token_hash)
            if login is not None:
                await session.delete(login)
                await session.commit()

    async def create_invite(self, **fields: Any) -> Invite:
        async with self._sessions() as session:
            invite = Invite(**fields)
            session.add(invite)
            await session.commit()
            return invite

    async def get_invite(self, token_hash: str) -> Invite | None:
        async with self._sessions() as session:
            invite = await session.get(Invite, token_hash)
            if invite is None or _as_utc(invite.expires_at) < now_utc():
                return None
            return invite

    async def list_invites(self, tenant_id: str) -> list[Invite]:
        async with self._sessions() as session:
            result = await session.execute(
                select(Invite).where(Invite.tenant_id == tenant_id, Invite.expires_at > now_utc())
            )
            return list(result.scalars())

    async def delete_invite(self, token_hash: str) -> None:
        async with self._sessions() as session:
            invite = await session.get(Invite, token_hash)
            if invite is not None:
                await session.delete(invite)
                await session.commit()

    async def latest_call_for_order(self, tenant_id: str, order_id: str) -> Call | None:
        async with self._sessions() as session:
            result = await session.execute(
                select(Call)
                .where(Call.tenant_id == tenant_id, Call.order_id == order_id)
                .order_by(Call.created_at.desc())
                .limit(1)
            )
            return result.scalars().first()

    async def update_tenant(self, tenant_id: str, **fields: Any) -> Tenant | None:
        async with self._sessions() as session:
            tenant = await session.get(Tenant, tenant_id)
            if tenant is None:
                return None
            for key, value in fields.items():
                setattr(tenant, key, value)
            await session.commit()
            return tenant

    # --------------------------------------------------------------- calls

    async def create(self, **fields: Any) -> Call:
        async with self._sessions() as session:
            call = Call(**fields)
            session.add(call)
            await session.commit()
            return call

    async def get(self, call_id: str, tenant_id: str | None = None) -> Call | None:
        async with self._sessions() as session:
            call = await session.get(Call, call_id)
            if call is not None and tenant_id is not None and call.tenant_id != tenant_id:
                return None
            return call

    async def get_by_provider_id(self, provider_call_id: str) -> Call | None:
        async with self._sessions() as session:
            result = await session.execute(
                select(Call).where(Call.provider_call_id == provider_call_id)
            )
            return result.scalars().first()

    async def get_by_idempotency_key(self, tenant_id: str, key: str) -> Call | None:
        async with self._sessions() as session:
            result = await session.execute(
                select(Call).where(Call.tenant_id == tenant_id, Call.idempotency_key == key)
            )
            return result.scalars().first()

    async def update(self, call_id: str, *, log: str | None = None, **fields: Any) -> Call | None:
        """Update fields; ``log`` also appends a timeline entry (with the new status)."""
        async with self._sessions() as session:
            call = await session.get(Call, call_id)
            if call is None:
                return None
            for key, value in fields.items():
                setattr(call, key, value)
            if log:
                entry = {"at": now_utc().isoformat(), "event": log}
                if "status" in fields:
                    entry["status"] = fields["status"]
                call.timeline = [*(call.timeline or []), entry]
            await session.commit()
            return call

    async def list_calls(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        tenant_id: str | None = None,
        status: str | None = None,
        event_type: str | None = None,
        order_id: str | None = None,
    ) -> list[Call]:
        query = select(Call)
        if tenant_id is not None:
            query = query.where(Call.tenant_id == tenant_id)
        if status:
            query = query.where(Call.status == status)
        if event_type:
            query = query.where(Call.event_type == event_type)
        if order_id:
            query = query.where(Call.order_id == order_id)
        async with self._sessions() as session:
            result = await session.execute(
                query.order_by(Call.created_at.desc()).limit(limit).offset(offset)
            )
            return list(result.scalars())

    async def due_calls(self, now: datetime, limit: int = 20) -> list[Call]:
        async with self._sessions() as session:
            result = await session.execute(
                select(Call)
                .where(Call.status == "scheduled", Call.next_attempt_at <= now)
                .order_by(Call.next_attempt_at)
                .limit(limit)
            )
            return list(result.scalars())

    async def count_active(self, tenant_id: str) -> int:
        async with self._sessions() as session:
            result = await session.execute(
                select(func.count())
                .select_from(Call)
                .where(Call.tenant_id == tenant_id, Call.status.in_(ACTIVE_STATUSES))
            )
            return int(result.scalar_one())

    async def stats(self, tenant_id: str) -> dict[str, dict[str, int]]:
        async with self._sessions() as session:
            out: dict[str, dict[str, int]] = {}
            for field in ("status", "outcome", "event_type"):
                column = getattr(Call, field)
                result = await session.execute(
                    select(column, func.count()).where(Call.tenant_id == tenant_id).group_by(column)
                )
                out[field] = {str(k): v for k, v in result.all() if k is not None}
            return out

    async def activity_since(self, tenant_id: str, since: datetime) -> list[tuple]:
        """(created_at, status, duration_secs) for a tenant's calls created since ``since``."""
        async with self._sessions() as session:
            result = await session.execute(
                select(Call.created_at, Call.status, Call.duration_secs).where(
                    Call.tenant_id == tenant_id, Call.created_at >= since
                )
            )
            return [(_as_utc(c), s, d) for c, s, d in result.all()]


def _add_missing_columns(sync_conn) -> None:
    """Tiny forward-only migration: add columns introduced after a table was created.

    Good enough while every new column is nullable/defaulted; switch to Alembic once
    real merchant data lives in production.
    """
    inspector = inspect(sync_conn)
    for table in Base.metadata.sorted_tables:
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            col_type = column.type.compile(dialect=sync_conn.dialect)
            sync_conn.execute(
                text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {col_type}')
            )
