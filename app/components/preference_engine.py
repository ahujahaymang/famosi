"""
Preference Engine — preference extraction, persistence, and conflict resolution.

Handles:
  - Structured extraction of preference type and food item from free-text messages (Req 5.1)
  - Confirmation-gated persistence with conflict detection (Req 5.2, 5.3, 5.5)
  - Retrieval of all active preferences for personalisation (Req 5.4, 5.6)

Conflict resolution design:
  The `save_preference` function does NOT interact with Telegram directly.
  It is a component — when a conflict is detected it returns a
  `PreferenceConflict` result that the calling bot handler must present to
  the user and resolve (via a follow-up confirmation).  On confirmation,
  the handler calls `replace_preference` to apply the change.

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.extractor import extract
from app.core.llm_client import LLMClient
from app.models.preference import Preference, PreferenceType
from app.schemas.preference import PreferenceExtraction

logger = structlog.get_logger(__name__)

# ── Public result types ──────────────────────────────────────────────────────


@dataclass
class PreferenceConflict:
    """
    Returned by :func:`save_preference` when a matching active preference
    already exists for the same ``(user_id, preference_type, food_item)``
    combination.

    The caller (bot handler) must present both preferences to the user and
    ask them to confirm which one should be kept.  If the user selects the
    new preference, pass this object to :func:`replace_preference`.

    Attributes:
        existing: The currently stored, active ``Preference`` ORM instance.
        incoming: The new extraction that triggered the conflict.
    """

    existing: Preference
    incoming: PreferenceExtraction


# ── Extraction ───────────────────────────────────────────────────────────────


async def extract_preference(
    message: str,
    llm_client: LLMClient,
) -> PreferenceExtraction | None:
    """
    Extract a food preference from a free-text user message.

    Delegates to the shared extraction pipeline using the "nano" tier and
    the ``PreferenceExtraction`` Pydantic schema.

    Args:
        message:    The raw user message to extract from.
        llm_client: Shared ``LLMClient`` instance.

    Returns:
        A validated ``PreferenceExtraction`` on success, or ``None`` when
        the LLM response cannot be parsed or required fields are missing.

    Requirements: 5.1
    """
    log = logger.bind(message_len=len(message))
    log.debug("preference_extraction_start")

    result = await extract("preference", message, llm_client)

    if result is None:
        log.warning("preference_extraction_failed", reason="hard_validation_failure")
        return None

    # MissingFields means preference_type or food_item could not be resolved
    if not isinstance(result, PreferenceExtraction):
        from app.core.extractor import MissingFields  # local import to avoid circular

        if isinstance(result, MissingFields):
            log.info(
                "preference_extraction_missing_fields",
                missing=result.missing,
            )
        else:
            log.warning(
                "preference_extraction_unexpected_result",
                result_type=type(result).__name__,
            )
        return None

    log.info(
        "preference_extraction_success",
        preference_type=result.preference_type,
        # food_item intentionally not logged (PII/health data)
    )
    return result


# ── Persistence ──────────────────────────────────────────────────────────────


async def save_preference(
    user_id: int,
    extraction: PreferenceExtraction,
    db: AsyncSession,
) -> Preference | PreferenceConflict:
    """
    Persist a confirmed food preference, detecting conflicts first.

    The caller MUST have already obtained explicit user confirmation for the
    ``extraction`` record before calling this function (Req 5.2).

    Conflict detection (Req 5.5):
        If an active ``Preference`` with the same ``(user_id, preference_type,
        food_item)`` already exists, this function does **not** overwrite it
        automatically.  Instead it returns a :class:`PreferenceConflict`
        containing both the existing and incoming records so the bot handler
        can present both to the user and request a selection.  The caller
        then calls :func:`replace_preference` if the user chooses the new one.

    If no conflict exists, a new ``Preference`` row is inserted and returned.

    Args:
        user_id:    The internal user PK.
        extraction: The validated ``PreferenceExtraction`` from the user message.
        db:         Active async DB session.

    Returns:
        - A newly inserted :class:`Preference` ORM instance when no conflict
          exists (Req 5.2, 5.3).
        - A :class:`PreferenceConflict` when an active preference for the same
          ``(type, food_item)`` already exists (Req 5.5).

    Requirements: 5.2, 5.3, 5.5
    """
    log = logger.bind(user_id=user_id, preference_type=extraction.preference_type)

    # Convert the string literal from the schema to the ORM enum
    pref_type_enum = PreferenceType(extraction.preference_type)

    # ── Conflict check ────────────────────────────────────────────────────────
    existing = await _find_active_preference(
        user_id=user_id,
        preference_type=pref_type_enum,
        food_item=extraction.food_item,
        db=db,
    )

    if existing is not None:
        log.info("preference_conflict_detected", existing_id=existing.id)
        return PreferenceConflict(existing=existing, incoming=extraction)

    # ── No conflict: insert new preference ───────────────────────────────────
    now_utc = datetime.now(timezone.utc)
    preference = Preference(
        user_id=user_id,
        preference_type=pref_type_enum,
        food_item=extraction.food_item,
        active=True,
        confirmed_at=now_utc,
    )
    db.add(preference)
    await db.flush()

    log.info("preference_saved", preference_id=preference.id)
    return preference


async def replace_preference(
    user_id: int,
    conflict: PreferenceConflict,
    db: AsyncSession,
) -> Preference:
    """
    Replace the existing preference in *conflict* with the incoming one.

    Called by the bot handler after the user has confirmed that the new
    preference should overwrite the existing one (Req 5.5).

    Steps:
    1. Set ``existing.active = False`` on the old row.
    2. Insert a fresh ``Preference`` row for the incoming extraction.
    3. Flush both changes.

    Args:
        user_id:  The internal user PK (validated by the caller).
        conflict: The :class:`PreferenceConflict` returned by
                  :func:`save_preference`.
        db:       Active async DB session.

    Returns:
        The newly inserted :class:`Preference` ORM instance.

    Requirements: 5.5
    """
    log = logger.bind(
        user_id=user_id,
        existing_id=conflict.existing.id,
        preference_type=conflict.incoming.preference_type,
    )

    # Deactivate the existing preference
    conflict.existing.active = False
    await db.flush()

    # Insert the replacement
    pref_type_enum = PreferenceType(conflict.incoming.preference_type)
    now_utc = datetime.now(timezone.utc)
    new_preference = Preference(
        user_id=user_id,
        preference_type=pref_type_enum,
        food_item=conflict.incoming.food_item,
        active=True,
        confirmed_at=now_utc,
    )
    db.add(new_preference)
    await db.flush()

    log.info("preference_replaced", new_preference_id=new_preference.id)
    return new_preference


# ── Retrieval ────────────────────────────────────────────────────────────────


async def get_active_preferences(
    user_id: int,
    db: AsyncSession,
) -> list[Preference]:
    """
    Return all active preferences for *user_id*.

    Used by Nutrition_Assistant, Cravings_Assistant, and any other component
    that must personalise its output based on stored preferences (Req 5.4, 5.6).

    Only rows with ``active = True`` are returned; deactivated (overwritten)
    preferences are excluded.

    Args:
        user_id: The internal user PK.
        db:      Active async DB session.

    Returns:
        A list of :class:`Preference` ORM instances ordered by ``created_at``
        ascending (oldest first, deterministic ordering for downstream use).

    Requirements: 5.4, 5.6
    """
    stmt = (
        select(Preference)
        .where(
            Preference.user_id == user_id,
            Preference.active.is_(True),
        )
        .order_by(Preference.created_at.asc())
    )
    result = await db.execute(stmt)
    preferences: list[Preference] = list(result.scalars().all())

    logger.debug(
        "active_preferences_retrieved",
        user_id=user_id,
        count=len(preferences),
    )
    return preferences


# ── Internal helpers ─────────────────────────────────────────────────────────


async def _find_active_preference(
    user_id: int,
    preference_type: PreferenceType,
    food_item: str,
    db: AsyncSession,
) -> Optional[Preference]:
    """
    Look up an active preference matching ``(user_id, preference_type, food_item)``.

    Returns the first matching :class:`Preference` or ``None`` if no match.
    The comparison on ``food_item`` is case-insensitive via ``ilike``.
    """
    stmt = (
        select(Preference)
        .where(
            Preference.user_id == user_id,
            Preference.preference_type == preference_type,
            Preference.food_item.ilike(food_item),
            Preference.active.is_(True),
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()
