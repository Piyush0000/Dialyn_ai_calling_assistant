"""Call records persistence (SQLite in dev, Postgres in prod)."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, String, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String(64), index=True)
    direction: Mapped[str] = mapped_column(String(16))  # inbound | outbound | web
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    provider: Mapped[str] = mapped_column(String(16), default="twilio")
    provider_call_id: Mapped[str | None] = mapped_column(String(64), index=True)
    from_number: Mapped[str | None] = mapped_column(String(32))
    to_number: Mapped[str | None] = mapped_column(String(32))
    variables: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    transcript: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    end_reason: Mapped[str | None] = mapped_column(String(64))
    recording_path: Mapped[str | None] = mapped_column(String(512))
    duration_secs: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def to_dict(self) -> dict[str, Any]:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class CallStore:
    def __init__(self, database_url: str):
        self.engine: AsyncEngine = create_async_engine(database_url)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        # Phase 1 convenience; switch to Alembic migrations before production data exists.
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    async def create(self, **fields: Any) -> Call:
        async with self._sessions() as session:
            call = Call(**fields)
            session.add(call)
            await session.commit()
            return call

    async def get(self, call_id: str) -> Call | None:
        async with self._sessions() as session:
            return await session.get(Call, call_id)

    async def get_by_provider_id(self, provider_call_id: str) -> Call | None:
        async with self._sessions() as session:
            result = await session.execute(
                select(Call).where(Call.provider_call_id == provider_call_id)
            )
            return result.scalars().first()

    async def update(self, call_id: str, **fields: Any) -> Call | None:
        async with self._sessions() as session:
            call = await session.get(Call, call_id)
            if call is None:
                return None
            for key, value in fields.items():
                setattr(call, key, value)
            await session.commit()
            return call

    async def list(self, limit: int = 50, offset: int = 0) -> list[Call]:
        async with self._sessions() as session:
            result = await session.execute(
                select(Call).order_by(Call.created_at.desc()).limit(limit).offset(offset)
            )
            return list(result.scalars())
