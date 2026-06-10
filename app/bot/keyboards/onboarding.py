"""
Inline keyboard builders for the onboarding conversation flow.

Three helper functions that return :class:`telegram.InlineKeyboardMarkup`
instances used at various stages of ``ConversationHandler``-driven
onboarding:

  - :func:`role_keyboard`           — choose Mom or Partner role
  - :func:`food_preference_keyboard` — choose dietary preference (matches
                                       the ``food_preference`` DB enum)
  - :func:`language_keyboard`       — choose interface language from
                                       common options (2-column layout)

All callback_data values follow the ``<category>:<value>`` convention so
that handlers can split on ``":"`` to extract the chosen value without
needing a global registry of constants.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def role_keyboard() -> InlineKeyboardMarkup:
    """
    Two-button inline keyboard for role selection.

    Buttons
    -------
    - "👶 Mom"     → callback_data ``"role:mom"``
    - "🤝 Partner" → callback_data ``"role:partner"``
    """
    keyboard = [
        [
            InlineKeyboardButton("👶 Mom", callback_data="role:mom"),
            InlineKeyboardButton("🤝 Partner", callback_data="role:partner"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def food_preference_keyboard() -> InlineKeyboardMarkup:
    """
    Five-button inline keyboard for food/dietary preference selection.

    Values match the ``food_preference`` PostgreSQL enum defined in
    :mod:`app.models.user` exactly:
    ``vegetarian``, ``vegan``, ``jain``, ``eggitarian``, ``non_vegetarian``.
    """
    keyboard = [
        [
            InlineKeyboardButton("🥗 Vegetarian", callback_data="food:vegetarian"),
            InlineKeyboardButton("🌱 Vegan", callback_data="food:vegan"),
        ],
        [
            InlineKeyboardButton("🪷 Jain", callback_data="food:jain"),
            InlineKeyboardButton("🥚 Eggitarian", callback_data="food:eggitarian"),
        ],
        [
            InlineKeyboardButton("🍗 Non-Vegetarian", callback_data="food:non_vegetarian"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def language_keyboard() -> InlineKeyboardMarkup:
    """
    Six-button inline keyboard for language selection, arranged in 2 columns.

    Common languages with their IANA language codes:

    +-------------+-----------+-------------------------------+
    | Language    | Code      | callback_data                 |
    +=============+===========+===============================+
    | English     | en        | ``"lang:en"``                 |
    | Spanish     | es        | ``"lang:es"``                 |
    | French      | fr        | ``"lang:fr"``                 |
    | Arabic      | ar        | ``"lang:ar"``                 |
    | Hindi       | hi        | ``"lang:hi"``                 |
    | Portuguese  | pt        | ``"lang:pt"``                 |
    +-------------+-----------+-------------------------------+
    """
    keyboard = [
        [
            InlineKeyboardButton("🇬🇧 English", callback_data="lang:en"),
            InlineKeyboardButton("🇪🇸 Spanish", callback_data="lang:es"),
        ],
        [
            InlineKeyboardButton("🇫🇷 French", callback_data="lang:fr"),
            InlineKeyboardButton("🇸🇦 Arabic", callback_data="lang:ar"),
        ],
        [
            InlineKeyboardButton("🇮🇳 Hindi", callback_data="lang:hi"),
            InlineKeyboardButton("🇧🇷 Portuguese", callback_data="lang:pt"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def yes_no_keyboard(prefix: str) -> InlineKeyboardMarkup:
    """
    Generic Yes / No inline keyboard.

    Parameters
    ----------
    prefix:
        The callback_data prefix, e.g. ``"first_preg"`` produces
        ``"first_preg:yes"`` and ``"first_preg:no"``.
    """
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes", callback_data=f"{prefix}:yes"),
            InlineKeyboardButton("❌ No", callback_data=f"{prefix}:no"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def country_keyboard() -> InlineKeyboardMarkup:
    """
    Inline keyboard of the most common countries, 2 per row.
    callback_data: ``"country:<ISO2>"``
    """
    countries = [
        ("🇮🇳 India", "IN"),
        ("🇺🇸 USA", "US"),
        ("🇬🇧 UK", "GB"),
        ("🇨🇦 Canada", "CA"),
        ("🇦🇺 Australia", "AU"),
        ("🇦🇪 UAE", "AE"),
        ("🇸🇬 Singapore", "SG"),
        ("🇩🇪 Germany", "DE"),
        ("🇫🇷 France", "FR"),
        ("🇧🇷 Brazil", "BR"),
        ("🇲🇽 Mexico", "MX"),
        ("🇳🇬 Nigeria", "NG"),
        ("🇵🇰 Pakistan", "PK"),
        ("🇧🇩 Bangladesh", "BD"),
        ("🇯🇵 Japan", "JP"),
        ("🇳🇿 New Zealand", "NZ"),
        ("🇿🇦 South Africa", "ZA"),
    ]
    keyboard: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for label, code in countries:
        row.append(InlineKeyboardButton(label, callback_data=f"country:{code}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)


def exercise_habit_keyboard() -> InlineKeyboardMarkup:
    """
    Inline keyboard for exercise habit level.
    callback_data: ``"exercise:<value>"``
    """
    keyboard = [
        [
            InlineKeyboardButton("🛋️ Not much / None", callback_data="exercise:none"),
            InlineKeyboardButton("🚶 Light walks", callback_data="exercise:light_walks"),
        ],
        [
            InlineKeyboardButton("🧘 Yoga / Stretching", callback_data="exercise:yoga"),
            InlineKeyboardButton("🏊 Swimming", callback_data="exercise:swimming"),
        ],
        [
            InlineKeyboardButton("🚴 Moderate (3x/week)", callback_data="exercise:moderate"),
            InlineKeyboardButton("🏃 Active (5x/week)", callback_data="exercise:active"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def wake_time_keyboard() -> InlineKeyboardMarkup:
    """
    Inline keyboard of common wake-up times in 30-min slots, 3 per row.
    callback_data: ``"wake:<HH:MM>"``
    """
    times = [
        "05:00", "05:30",
        "06:00", "06:30",
        "07:00", "07:30",
        "08:00", "08:30",
        "09:00", "09:30",
        "10:00",
    ]
    labels = {
        "05:00": "5:00 AM", "05:30": "5:30 AM",
        "06:00": "6:00 AM", "06:30": "6:30 AM",
        "07:00": "7:00 AM", "07:30": "7:30 AM",
        "08:00": "8:00 AM", "08:30": "8:30 AM",
        "09:00": "9:00 AM", "09:30": "9:30 AM",
        "10:00": "10:00 AM",
    }
    keyboard: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for t in times:
        row.append(InlineKeyboardButton(labels[t], callback_data=f"wake:{t}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)


def sleep_time_keyboard() -> InlineKeyboardMarkup:
    """
    Inline keyboard of common sleep times in 30-min slots, 3 per row.
    callback_data: ``"sleep:<HH:MM>"``
    """
    times = [
        "20:00", "20:30",
        "21:00", "21:30",
        "22:00", "22:30",
        "23:00", "23:30",
        "00:00",
    ]
    labels = {
        "20:00": "8:00 PM", "20:30": "8:30 PM",
        "21:00": "9:00 PM", "21:30": "9:30 PM",
        "22:00": "10:00 PM", "22:30": "10:30 PM",
        "23:00": "11:00 PM", "23:30": "11:30 PM",
        "00:00": "Midnight",
    }
    keyboard: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for t in times:
        row.append(InlineKeyboardButton(labels[t], callback_data=f"sleep:{t}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)


def support_prefs_keyboard() -> InlineKeyboardMarkup:
    """
    Inline keyboard for partner support preference selection.
    callback_data: ``"support:<value>"``
    """
    keyboard = [
        [
            InlineKeyboardButton("🔔 Appointment reminders", callback_data="support:reminders"),
            InlineKeyboardButton("💬 Daily check-ins", callback_data="support:checkins"),
        ],
        [
            InlineKeyboardButton("🍎 Nutrition tips", callback_data="support:nutrition"),
            InlineKeyboardButton("📅 Milestone tracking", callback_data="support:milestones"),
        ],
        [
            InlineKeyboardButton("🌟 All of the above", callback_data="support:all"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


# ---------------------------------------------------------------------------
# Timezone selection — grouped by country code
# ---------------------------------------------------------------------------

# Common timezone options per country (ISO 3166-1 alpha-2 → list of (label, tz))
_COUNTRY_TIMEZONES: dict[str, list[tuple[str, str]]] = {
    "IN": [("🇮🇳 India (IST)", "Asia/Kolkata")],
    "US": [
        ("🌅 Eastern (ET)", "America/New_York"),
        ("🏔️ Central (CT)", "America/Chicago"),
        ("⛰️ Mountain (MT)", "America/Denver"),
        ("🌊 Pacific (PT)", "America/Los_Angeles"),
        ("🌺 Hawaii (HT)", "Pacific/Honolulu"),
        ("🏔️ Alaska (AKT)", "America/Anchorage"),
    ],
    "GB": [("🇬🇧 UK (GMT/BST)", "Europe/London")],
    "CA": [
        ("🌅 Eastern (ET)", "America/Toronto"),
        ("🏔️ Central (CT)", "America/Winnipeg"),
        ("⛰️ Mountain (MT)", "America/Edmonton"),
        ("🌊 Pacific (PT)", "America/Vancouver"),
    ],
    "AU": [
        ("🌊 AWST Perth", "Australia/Perth"),
        ("⛰️ ACST Adelaide", "Australia/Adelaide"),
        ("🌅 AEST Sydney", "Australia/Sydney"),
        ("🌅 AEST Brisbane", "Australia/Brisbane"),
    ],
    "DE": [("🇩🇪 Germany (CET)", "Europe/Berlin")],
    "FR": [("🇫🇷 France (CET)", "Europe/Paris")],
    "AE": [("🇦🇪 UAE (GST)", "Asia/Dubai")],
    "SG": [("🇸🇬 Singapore (SGT)", "Asia/Singapore")],
    "JP": [("🇯🇵 Japan (JST)", "Asia/Tokyo")],
    "NZ": [("🇳🇿 New Zealand (NZST)", "Pacific/Auckland")],
    "ZA": [("🇿🇦 South Africa (SAST)", "Africa/Johannesburg")],
    "BR": [
        ("🌅 Brasília (BRT)", "America/Sao_Paulo"),
        ("🌊 Manaus (AMT)", "America/Manaus"),
    ],
    "MX": [
        ("🌅 Mexico City (CST)", "America/Mexico_City"),
        ("🌊 Tijuana (PST)", "America/Tijuana"),
    ],
    "PK": [("🇵🇰 Pakistan (PKT)", "Asia/Karachi")],
    "BD": [("🇧🇩 Bangladesh (BST)", "Asia/Dhaka")],
    "NG": [("🇳🇬 Nigeria (WAT)", "Africa/Lagos")],
}

# Fallback list for countries not in the map — covers all major zones
_FALLBACK_TIMEZONES: list[tuple[str, str]] = [
    ("UTC+0 (UTC)", "UTC"),
    ("UTC+1 (WAT/CET)", "Europe/Paris"),
    ("UTC+2 (EET/SAST)", "Africa/Johannesburg"),
    ("UTC+3 (MSK/EAT)", "Europe/Moscow"),
    ("UTC+3:30 (IRST)", "Asia/Tehran"),
    ("UTC+4 (GST/AMT)", "Asia/Dubai"),
    ("UTC+4:30 (AFT)", "Asia/Kabul"),
    ("UTC+5 (PKT/UZT)", "Asia/Karachi"),
    ("UTC+5:30 (IST)", "Asia/Kolkata"),
    ("UTC+5:45 (NPT)", "Asia/Kathmandu"),
    ("UTC+6 (BST/OMST)", "Asia/Dhaka"),
    ("UTC+7 (ICT/WIB)", "Asia/Bangkok"),
    ("UTC+8 (CST/SGT)", "Asia/Singapore"),
    ("UTC+9 (JST/KST)", "Asia/Tokyo"),
    ("UTC+9:30 (ACST)", "Australia/Adelaide"),
    ("UTC+10 (AEST)", "Australia/Sydney"),
    ("UTC+12 (NZST)", "Pacific/Auckland"),
    ("UTC-5 (EST)", "America/New_York"),
    ("UTC-6 (CST)", "America/Chicago"),
    ("UTC-7 (MST)", "America/Denver"),
    ("UTC-8 (PST)", "America/Los_Angeles"),
    ("UTC-3 (BRT)", "America/Sao_Paulo"),
]


def timezone_keyboard(country_code: str | None = None) -> InlineKeyboardMarkup:
    """
    Build a timezone selection keyboard.

    If *country_code* is known, shows only timezones for that country.
    Otherwise shows a global list of common UTC offsets.

    Each button callback_data is ``"tz:<iana_timezone>"``.
    Buttons are arranged two per row.
    """
    code = (country_code or "").upper()
    options = _COUNTRY_TIMEZONES.get(code, _FALLBACK_TIMEZONES)

    keyboard: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for label, tz in options:
        row.append(InlineKeyboardButton(label, callback_data=f"tz:{tz}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    return InlineKeyboardMarkup(keyboard)


__all__ = [
    "role_keyboard",
    "food_preference_keyboard",
    "language_keyboard",
    "yes_no_keyboard",
    "country_keyboard",
    "exercise_habit_keyboard",
    "wake_time_keyboard",
    "sleep_time_keyboard",
    "support_prefs_keyboard",
    "timezone_keyboard",
]
