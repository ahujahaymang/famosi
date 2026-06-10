"""
Inline keyboard builder for record visibility selection.

Visibility levels map directly to the ``visibility_level`` PostgreSQL enum:
``private``, ``partner_shared``, ``doctor_shared``.

Exports
-------
- :data:`VISIBILITY_PRIVATE`       — callback_data for "private"
- :data:`VISIBILITY_PARTNER`       — callback_data for "partner_shared"
- :data:`VISIBILITY_DOCTOR`        — callback_data for "doctor_shared"
- :data:`VALID_VISIBILITY_VALUES`  — frozenset of all valid callback_data strings
- :data:`INVALID_VISIBILITY_MESSAGE` — user-facing error message (Req 6.7)
- :func:`build_visibility_keyboard` — returns a column-layout InlineKeyboardMarkup
- :func:`validate_visibility_callback` — validates a callback_data string
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# ---------------------------------------------------------------------------
# Callback data constants — follow the ``<category>:<value>`` convention used
# throughout the keyboards package so handlers can split on ":" to extract
# the chosen value without a global registry lookup.
# ---------------------------------------------------------------------------

VISIBILITY_PRIVATE: str = "visibility:private"
VISIBILITY_PARTNER: str = "visibility:partner_shared"
VISIBILITY_DOCTOR: str = "visibility:doctor_shared"

VALID_VISIBILITY_VALUES: frozenset[str] = frozenset(
    {VISIBILITY_PRIVATE, VISIBILITY_PARTNER, VISIBILITY_DOCTOR}
)

# User-facing message shown when an unrecognised callback is received (Req 6.7).
INVALID_VISIBILITY_MESSAGE: str = (
    "Please choose one of: Private, Partner Shared, or Doctor Shared."
)


def build_visibility_keyboard() -> InlineKeyboardMarkup:
    """
    Column-layout inline keyboard for visibility level selection.

    Each button is placed on its own row so the labels remain readable on
    narrow mobile screens.

    Buttons
    -------
    - "🔒 Private"         → callback_data ``"visibility:private"``
    - "👫 Partner Shared"  → callback_data ``"visibility:partner_shared"``
    - "👨‍⚕️ Doctor Shared"  → callback_data ``"visibility:doctor_shared"``

    Returns
    -------
    :class:`telegram.InlineKeyboardMarkup`
    """
    keyboard = [
        [InlineKeyboardButton("🔒 Private", callback_data=VISIBILITY_PRIVATE)],
        [InlineKeyboardButton("👫 Partner Shared", callback_data=VISIBILITY_PARTNER)],
        [InlineKeyboardButton("👨‍⚕️ Doctor Shared", callback_data=VISIBILITY_DOCTOR)],
    ]
    return InlineKeyboardMarkup(keyboard)


def validate_visibility_callback(callback_data: str) -> bool:
    """
    Return ``True`` when *callback_data* is one of the three valid visibility
    values; ``False`` otherwise.

    Use this in callback query handlers to guard against unexpected or
    tampered callback payloads before mapping to the DB enum (Req 6.7).

    Parameters
    ----------
    callback_data:
        The raw ``data`` field from a :class:`telegram.CallbackQuery`.

    Returns
    -------
    bool
    """
    return callback_data in VALID_VISIBILITY_VALUES


__all__ = [
    "VISIBILITY_PRIVATE",
    "VISIBILITY_PARTNER",
    "VISIBILITY_DOCTOR",
    "VALID_VISIBILITY_VALUES",
    "INVALID_VISIBILITY_MESSAGE",
    "build_visibility_keyboard",
    "validate_visibility_callback",
]
