"""
Inline keyboard builder for the confirmation step of the logging flow.

Presents three actions after the bot displays an extracted-data summary:

  - :data:`CONFIRM_SAVE`   — persist the record
  - :data:`CONFIRM_EDIT`   — re-prompt for corrections (increments edit count)
  - :data:`CONFIRM_CANCEL` — discard the pending record

Callback data follows the ``<category>:<action>`` convention used throughout
the keyboards package so handlers can split on ``":"`` to identify the action.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# ---------------------------------------------------------------------------
# Callback data constants
# ---------------------------------------------------------------------------

CONFIRM_SAVE = "confirm:save"
CONFIRM_EDIT = "confirm:edit"
CONFIRM_CANCEL = "confirm:cancel"


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_confirm_keyboard() -> InlineKeyboardMarkup:
    """
    Three-button inline keyboard for confirming an extracted health record.

    All three buttons are placed in a single row:

    +------------+-----------+------------+
    | 💾 Save    | ✏️ Edit   | ❌ Cancel  |
    +------------+-----------+------------+

    Returns
    -------
    :class:`telegram.InlineKeyboardMarkup`
    """
    keyboard = [
        [
            InlineKeyboardButton("💾 Save", callback_data=CONFIRM_SAVE),
            InlineKeyboardButton("✏️ Edit", callback_data=CONFIRM_EDIT),
            InlineKeyboardButton("❌ Cancel", callback_data=CONFIRM_CANCEL),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


__all__ = [
    "CONFIRM_SAVE",
    "CONFIRM_EDIT",
    "CONFIRM_CANCEL",
    "build_confirm_keyboard",
]
