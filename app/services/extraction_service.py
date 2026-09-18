"""Extraction service: transcript lifecycle plus extraction orchestration.

Why the LLM call happens *outside* a database transaction
--------------------------------------------------------
The transcript is read in a short session that closes immediately. The Claude call
(a slow network operation taking seconds) then runs with no DB transaction open,
and a second short session persists the results.

Holding a transaction across an external HTTP call is one of the most common
production mistakes: it pins a database connection, blocks other readers on
SQLite, and turns a slow third-party into a database outage. Keeping transactions
short also means a Claude failure cannot leave a half-written extraction behind.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.agents.extraction_agent import ExtractionAgent, prepare_transcript
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.database import Database
from app.db.repository import ActionItemRepository, TranscriptRepository
from app.models.action_item import ActionItem
from app.models.transcript import Transcript


class ExtractionService:
    """Stores transcripts and turns them into reviewable action items."""

    def __init__(self, database: Database, agent: ExtractionAgent, settings: Settings) -> None:
        self._db = database
        self._agent = agent
        self._settings = settings
        self._logger = get_logger(__name__)

    # ------------------------------------------------------------------
    # Transcripts
    # ------------------------------------------------------------------
    def create_transcript(
        self,
        *,
        content: str,
        title: str | None = None,
        meeting_date: datetime | None = None,
    ) -> Transcript:
        """Validate and store a transcript.

        Validation happens before persisting so an empty or oversized transcript
        never becomes a row that can only ever fail extraction.
        """
        cleaned = prepare_transcript(content, self._settings)
        transcript = Transcript(title=title, content=cleaned, meeting_date=meeting_date)
        with self._db.session() as session:
            TranscriptRepository(session).add(transcript)
        self._logger.info(
            "Transcript stored",
            extra={"transcript_id": str(transcript.id), "char_count": transcript.char_count},
        )
        return transcript

    def get_transcript(self, transcript_id: UUID | str) -> Transcript:
        with self._db.session() as session:
            return TranscriptRepository(session).require(transcript_id)

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------
    async def extract(self, transcript_id: UUID | str) -> list[ActionItem]:
        """Run the extraction agent for a stored transcript and persist the items."""
        transcript = self.get_transcript(transcript_id)

        # No transaction is open here on purpose (see module docstring).
        extracted = await self._agent.extract(
            transcript.content,
            meeting_title=transcript.title,
            meeting_date=transcript.meeting_date,
        )

        items = [
            ActionItem.from_extracted(item, transcript_id=transcript.id) for item in extracted
        ]

        with self._db.session() as session:
            ActionItemRepository(session).add_many(items)

        self._logger.info(
            "Action items persisted",
            extra={"transcript_id": str(transcript.id), "item_count": len(items)},
        )
        return items