"""
Confirmation loop session management.

Manages pending confirmation sessions — the short-lived state between when the
Bot presents an extraction summary and when the user presses Save, Edit, or Cancel.

Key design decisions:
- Sessions are stored in Redis (with a 300-second TTL) when available.
- When Redis is unavailable the module falls back to an in-process asyncio-safe
  dict.  Both paths provide identical semantics; the in-process dict trades
  durability for zero-dependency operation on a single-process deployment.
- Sessions expire after 5 minutes regardless of storage backend (Req 19.3).
- After 3 or more edit/reject cycles the session is discarded and the user is
  informed (Req 4.5).
- All public methods are async so they compose naturally with FastAPI / PTB
  async handlers.

Requirements: 4.3, 4.4, 4.5, 19.1, 19.2, 19.3
"""

from __future__ import annotations

import asyncio
import pickle
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: TTL in seconds for every confirmation session (Req 19.3 — 5-minute timeout)
SESSION_TTL_SECONDS: int = 300

#: Maximum number of edit/reject cycles before the session is force-discarded
#: (Req 4.5 — discard after 3 or more rejections)
MAX_EDIT_COUNT: int = 3

# Redis key prefix used for all confirmation sessions
_REDIS_KEY_PREFIX = "confirm:"


# ---------------------------------------------------------------------------
# ConfirmationSession dataclass
# ---------------------------------------------------------------------------


@dataclass
class ConfirmationSession:
    """
    Represents a pending user confirmation for a health-record extraction.

    Attributes:
        record: The extracted Pydantic model (e.g. MealExtraction) awaiting
                user confirmation.  Any serialisable object is accepted.
        edit_count: Number of Edit/Reject cycles the user has performed in
                    this session.  When this reaches MAX_EDIT_COUNT the
                    session must be discarded (Req 4.5).
        created_at: UTC datetime when the session was first created.  Used to
                    detect the 5-minute expiry independently of the Redis TTL
                    so the in-process fallback has the same semantics.
    """

    record: Any
    edit_count: int = 0
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def is_timed_out(self, now: Optional[datetime] = None) -> bool:
        """
        Return True when the session has exceeded the 5-minute TTL.

        Args:
            now: Override "current time" (UTC-aware). Defaults to
                 ``datetime.now(timezone.utc)``.  Useful in tests.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        elapsed = (now - self.created_at).total_seconds()
        return elapsed >= SESSION_TTL_SECONDS

    def is_over_edit_limit(self) -> bool:
        """Return True when the user has exhausted all edit/reject cycles."""
        return self.edit_count >= MAX_EDIT_COUNT


# ---------------------------------------------------------------------------
# Internal serialisation helpers
# ---------------------------------------------------------------------------


def _serialize(session: ConfirmationSession) -> bytes:
    """
    Serialise a ConfirmationSession to bytes for Redis storage.

    Uses pickle so that arbitrary Pydantic model instances (which cannot
    always round-trip cleanly through plain JSON) are preserved exactly.
    """
    return pickle.dumps(session)


def _deserialize(data: bytes) -> ConfirmationSession:
    """Deserialise bytes (produced by _serialize) back to a ConfirmationSession."""
    return pickle.loads(data)  # noqa: S301 — data originates from our own process


# ---------------------------------------------------------------------------
# In-process fallback store
# ---------------------------------------------------------------------------


class _InProcessStore:
    """
    asyncio-safe in-process dict that mimics the Redis TTL semantics.

    Each entry is a ``(session, expiry_utc)`` pair.  Expired entries are
    evicted lazily on every read to avoid leaking memory in long-running
    processes.  This is intentionally simple — it is only used when Redis
    is unavailable.
    """

    def __init__(self) -> None:
        self._data: dict[str, tuple[ConfirmationSession, datetime]] = {}
        self._lock = asyncio.Lock()

    async def put(self, session_id: str, session: ConfirmationSession, ttl: int) -> None:
        expiry = datetime.now(timezone.utc)
        from datetime import timedelta
        expiry = expiry + timedelta(seconds=ttl)
        async with self._lock:
            self._data[session_id] = (session, expiry)

    async def get(self, session_id: str) -> Optional[ConfirmationSession]:
        async with self._lock:
            entry = self._data.get(session_id)
            if entry is None:
                return None
            session, expiry = entry
            if datetime.now(timezone.utc) >= expiry:
                del self._data[session_id]
                return None
            return session

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._data.pop(session_id, None)


# Module-level singleton — created once and reused across all requests when
# Redis is unavailable.
_in_process_store = _InProcessStore()


# ---------------------------------------------------------------------------
# ConfirmationStore — public API
# ---------------------------------------------------------------------------


class ConfirmationStore:
    """
    Stores and retrieves pending confirmation sessions.

    Uses Redis as the primary backend when a connected async Redis client is
    provided.  Falls back to an asyncio-safe in-process dict otherwise.

    Intended usage (per incoming Telegram update)::

        store = ConfirmationStore(redis=redis_client)  # redis may be None

        # After extraction — store the session
        session = ConfirmationSession(record=extracted_model)
        await store.put(session_id, session)

        # On callback — retrieve and act
        session = await store.get(session_id)
        if session is None:
            # expired or already deleted
            ...

        # After save or final discard
        await store.delete(session_id)

    Args:
        redis: An async Redis client (e.g. ``redis.asyncio.Redis``).
               Pass ``None`` (default) to use the in-process fallback.
    """

    def __init__(self, redis: Any = None) -> None:
        self._redis = redis
        self._use_redis = redis is not None

    # ------------------------------------------------------------------
    # put
    # ------------------------------------------------------------------

    async def put(
        self,
        session_id: str,
        session: ConfirmationSession,
    ) -> None:
        """
        Store a ConfirmationSession with a 5-minute TTL.

        If a session already exists for *session_id* it is overwritten — this
        is the expected behaviour on Edit cycles where the session is updated
        with a new ``record`` and incremented ``edit_count``.

        Args:
            session_id: Unique identifier for the session (typically built
                        from the user's Telegram ID so at most one pending
                        confirmation exists per user).
            session: The session to store.
        """
        log = logger.bind(session_id=session_id, edit_count=session.edit_count)

        if self._use_redis:
            try:
                key = _REDIS_KEY_PREFIX + session_id
                await self._redis.setex(
                    key,
                    SESSION_TTL_SECONDS,
                    _serialize(session),
                )
                log.debug("confirmation_session_stored_redis")
                return
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "confirmation_redis_put_failed_falling_back",
                    error=str(exc),
                )
                # Fall through to in-process store

        await _in_process_store.put(session_id, session, SESSION_TTL_SECONDS)
        log.debug("confirmation_session_stored_in_process")

    # ------------------------------------------------------------------
    # get
    # ------------------------------------------------------------------

    async def get(
        self,
        session_id: str,
        now: Optional[datetime] = None,
    ) -> Optional[ConfirmationSession]:
        """
        Retrieve a ConfirmationSession, or ``None`` if it has expired or
        does not exist.

        Expiry is checked both via the storage-backend TTL (Redis) and via the
        ``created_at`` timestamp on the session itself, so the in-process
        fallback also honours the 5-minute limit correctly (Req 19.3).

        Args:
            session_id: The session identifier used in ``put``.
            now: Override "now" for testing.  Defaults to
                 ``datetime.now(timezone.utc)``.

        Returns:
            The session, or ``None`` if expired / not found.
        """
        log = logger.bind(session_id=session_id)

        session: Optional[ConfirmationSession] = None

        if self._use_redis:
            try:
                key = _REDIS_KEY_PREFIX + session_id
                raw = await self._redis.get(key)
                if raw is not None:
                    session = _deserialize(raw)
                    log.debug("confirmation_session_retrieved_redis")
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "confirmation_redis_get_failed_falling_back",
                    error=str(exc),
                )
                # Fall through to in-process store

        if session is None:
            session = await _in_process_store.get(session_id)
            if session is not None:
                log.debug("confirmation_session_retrieved_in_process")

        if session is None:
            log.debug("confirmation_session_not_found")
            return None

        # Double-check the wall-clock expiry on the session object itself
        # (provides defence-in-depth even if the Redis TTL somehow fired early
        # or the in-process store's expiry calculation drifts).
        if session.is_timed_out(now=now):
            log.info("confirmation_session_expired_on_get")
            await self.delete(session_id)
            return None

        return session

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    async def delete(self, session_id: str) -> None:
        """
        Remove a session from the store.

        Should be called after a successful save *or* after a discard (timeout,
        cancel, or 3rd rejection) so stale sessions do not accumulate.

        Args:
            session_id: The session identifier to remove.
        """
        log = logger.bind(session_id=session_id)

        if self._use_redis:
            try:
                key = _REDIS_KEY_PREFIX + session_id
                await self._redis.delete(key)
                log.debug("confirmation_session_deleted_redis")
                return
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "confirmation_redis_delete_failed",
                    error=str(exc),
                )

        await _in_process_store.delete(session_id)
        log.debug("confirmation_session_deleted_in_process")


# ---------------------------------------------------------------------------
# Business-logic helpers
# ---------------------------------------------------------------------------


async def check_and_handle_discard(
    store: ConfirmationStore,
    session_id: str,
    now: Optional[datetime] = None,
) -> tuple[Optional[ConfirmationSession], bool]:
    """
    Retrieve a session and evaluate whether it should be discarded.

    This helper consolidates the two discard conditions so that bot handlers
    can act on a single result rather than re-implementing the logic:

    - **Timeout** (Req 19.3): session older than 300 seconds → discard.
    - **Edit limit** (Req 4.5): ``edit_count >= MAX_EDIT_COUNT`` → discard.

    The session is automatically deleted from the store in either case.

    Args:
        store: The ConfirmationStore to query.
        session_id: Session identifier.
        now: Override "now" for testing.

    Returns:
        A ``(session, should_discard)`` tuple.

        - ``(None, True)``    — not found or already expired (already gone
                                from the store).
        - ``(session, True)`` — over the edit limit; caller should inform the
                                user and not re-present the summary.
        - ``(session, False)``— session is valid and below the edit limit;
                                caller should proceed normally.
    """
    session = await store.get(session_id, now=now)

    if session is None:
        # Either timed-out (get() already deleted it) or never existed
        return None, True

    if session.is_over_edit_limit():
        logger.info(
            "confirmation_session_edit_limit_reached",
            session_id=session_id,
            edit_count=session.edit_count,
        )
        await store.delete(session_id)
        return session, True

    return session, False
