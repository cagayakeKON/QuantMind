"""Legacy JP source archive retained for migration/provenance verification.

New accounts and execution use the common simulation/replay tables. No runtime
endpoint or worker creates, queues orders in, or advances these legacy rows.
"""

import uuid
from datetime import date

from sqlalchemy import Date, Index, Integer, JSON, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.services.simulation.models import Base, TimestampMixin


class JPSimulationSession(Base, TimestampMixin):
    __tablename__ = "jp_simulation_sessions"

    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(
        String(128), nullable=False, default="日股模拟账户"
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    anchor_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    data_version: Mapped[str] = mapped_column(String(96), nullable=False)
    state: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    pending: Mapped[list] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=list
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("idx_jp_simulation_scope", "tenant_id", "user_id", "mode"),)
