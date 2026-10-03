"""Immutable import provenance, independent of disposable replay sessions."""

from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.services.simulation.models import Base
from backend.shared.utc_datetime import UtcDateTime, utc_now


class ReplayImportReceipt(Base):
    __tablename__ = "replay_import_receipts"

    # Deliberately no FK: the original DELETE/CASCADE lifecycle must remain free
    # to discard a replay without undoing completion of its one-time migration.
    session_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    market: Mapped[str] = mapped_column(String(16), nullable=False)
    data_version: Mapped[str] = mapped_column(String(96), nullable=False)
    source_format: Mapped[str] = mapped_column(String(64), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utc_now, nullable=False
    )
