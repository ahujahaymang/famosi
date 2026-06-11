"""
All 100 evaluation scenarios as structured data.

Each scenario is a dict with:
  id:        int — unique identifier
  category:  str — grouping for reporting
  name:      str — short description
  messages:  list[str] — one or more user messages to send in sequence
  setup:     dict — DB state / user config required before running
             {
               "role": "mom" | "partner",
               "lmp_days_ago": int,      # LMP was N days ago
               "due_days_from_now": int, # or specify due date
               "timezone": str,
               "food_preference": str,
               "pre_log": list[dict],    # records to insert before the test
               "family": bool,           # True = create a linked family unit
             }
  expected:  list[str] — criteria the judge evaluates against (ALL must be met for PASS)
  tags:      list[str] — for filtering
"""
from __future__ import annotations
from typing import Any

Scenario = dict[str, Any]

# Default setup used when a scenario doesn't specify overrides
_DEFAULT_MOM = {
    "role": "mom",
    "lmp_days_ago": 60,        # ~8.5 weeks pregnant
    "timezone": "Asia/Kolkata",
    "food_preference": "vegetarian",
    "approved": True,
}

_DEFAULT_PARTNER = {
    "role": "partner",
    "lmp_days_ago": 60,
    "timezone": "Asia/Kolkata",
    "approved": True,
    "family": True,
}


SCENARIOS: list[Scenario] = [

    # =========================================================================
    # Category 1: Onboarding (1-15)
    # =========================================================================

    {
        "id": 1,
        "category": "Onboarding",
        "name": "New Mom Registration — /start shows role selection",
        "messages": ["/start"],
        "setup": {"role": None},  # no user in DB yet
        "expected": [
            "Presents a role selection (mom / partner)",
            "Does not skip straight to a form",
        ],
        "tags": ["onboarding", "first-run"],
    },
    {
        "id": 2,
        "category": "Onboarding",
        "name": "Privacy Consent — policy shown, cannot skip",
        "messages": ["/consent"],
        "setup": _DEFAULT_MOM | {"consent": False},
        "expected": [
            "Displays privacy policy text",
            "Shows accept and decline buttons",
            "Does not proceed without consent",
        ],
        "tags": ["onboarding", "consent"],
    },
    {
        "id": 3,
        "category": "Onboarding",
        "name": "Trial Activation — approval pending then trial starts",
        "messages": ["I had oatmeal for breakfast"],
        "setup": _DEFAULT_MOM | {"approved": False, "approval_pending": True},
        "expected": [
            "User is told they are awaiting approval",
            "No health data is logged or extracted",
        ],
        "tags": ["onboarding", "approval"],
    },
    {
        "id": 4,
        "category": "Onboarding",
        "name": "Vegetarian selection stored",
        "messages": ["Vegetarian"],
        "setup": _DEFAULT_MOM | {"onboarding_step": "FOOD_PREFERENCE"},
        "expected": [
            "Vegetarian preference is acknowledged or stored",
        ],
        "tags": ["onboarding", "preference"],
    },
    {
        "id": 5,
        "category": "Onboarding",
        "name": "Exercise habit stored",
        "messages": ["Walk 30 mins every morning"],
        "setup": _DEFAULT_MOM | {"onboarding_step": "EXERCISE_HABIT"},
        "expected": [
            "Exercise habit is acknowledged or stored",
        ],
        "tags": ["onboarding"],
    },
    {
        "id": 6,
        "category": "Onboarding",
        "name": "Timezone stored correctly",
        "messages": ["US Pacific"],
        "setup": _DEFAULT_MOM | {"onboarding_step": "TIMEZONE"},
        "expected": [
            "Pacific timezone (America/Los_Angeles) is acknowledged or stored",
        ],
        "tags": ["onboarding", "timezone"],
    },
    {
        "id": 7,
        "category": "Onboarding",
        "name": "Re-onboarding shows confirmation before deleting data",
        "messages": ["/start"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Warns user that data will be permanently deleted",
            "Shows yes/no confirmation buttons",
            "Does NOT delete data without confirmation",
        ],
        "tags": ["onboarding", "reset"],
    },
    {
        "id": 8,
        "category": "Onboarding",
        "name": "Dad/Partner registration — partner-specific flow",
        "messages": ["/start"],
        "setup": {"role": None, "force_role": "partner"},
        "expected": [
            "Presents role selection including partner option",
            "Partner flow asks for LMP date (not due date)",
        ],
        "tags": ["onboarding", "partner"],
    },
    {
        "id": 9,
        "category": "Onboarding",
        "name": "Invite code creates family link",
        "messages": ["/invite"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Generates a 6-character invite code",
            "Instructs user to share it with their partner",
        ],
        "tags": ["onboarding", "family"],
    },
    {
        "id": 10,
        "category": "Onboarding",
        "name": "Invalid invite code gives helpful error",
        "messages": ["ZZZZZZ"],
        "setup": {"role": "partner", "onboarding_step": "INVITE_CODE"},
        "expected": [
            "Tells user the code is invalid or already used",
            "Offers to skip or try again",
        ],
        "tags": ["onboarding", "family"],
    },
    {
        "id": 11,
        "category": "Onboarding",
        "name": "Language selection stored",
        "messages": ["English"],
        "setup": _DEFAULT_MOM | {"onboarding_step": "LANGUAGE"},
        "expected": [
            "Language preference is acknowledged",
        ],
        "tags": ["onboarding"],
    },
    {
        "id": 12,
        "category": "Onboarding",
        "name": "First pregnancy flag stored",
        "messages": ["Yes"],
        "setup": _DEFAULT_MOM | {"onboarding_step": "FIRST_PREGNANCY"},
        "expected": [
            "First pregnancy is acknowledged or stored",
        ],
        "tags": ["onboarding"],
    },
    {
        "id": 13,
        "category": "Onboarding",
        "name": "Sleep schedule saved",
        "messages": ["10pm"],
        "setup": _DEFAULT_MOM | {"onboarding_step": "SLEEP_TIME"},
        "expected": [
            "Sleep time is acknowledged or stored",
        ],
        "tags": ["onboarding"],
    },
    {
        "id": 14,
        "category": "Onboarding",
        "name": "Wake schedule saved",
        "messages": ["7am"],
        "setup": _DEFAULT_MOM | {"onboarding_step": "WAKE_TIME"},
        "expected": [
            "Wake time is acknowledged or stored",
        ],
        "tags": ["onboarding"],
    },
    {
        "id": 15,
        "category": "Onboarding",
        "name": "Consent declined — no health data collected",
        "messages": ["/consent"],
        "setup": _DEFAULT_MOM | {"consent": False},
        "expected": [
            "Consent can be declined",
            "User is informed they cannot log health data without consent",
        ],
        "tags": ["onboarding", "consent"],
    },

    # =========================================================================
    # Category 2: Logging & Confirmation (16-35)
    # =========================================================================

    {
        "id": 16,
        "category": "Logging & Confirmation",
        "name": "Meal extracted with confirmation",
        "messages": ["Had oatmeal and banana for breakfast"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Extracts oatmeal and banana as meal items",
            "Shows a confirmation summary with Save/Edit/Cancel",
            "Does NOT save without confirmation",
        ],
        "tags": ["logging", "meal"],
    },
    {
        "id": 17,
        "category": "Logging & Confirmation",
        "name": "Meal save persists",
        "messages": ["Had oatmeal and banana for breakfast", "CONFIRM_SAVE"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Meal is saved",
            "Confirmation shows saved successfully",
            "Visibility option offered",
        ],
        "tags": ["logging", "meal", "confirm"],
    },
    {
        "id": 18,
        "category": "Logging & Confirmation",
        "name": "Edit flow after extraction",
        "messages": ["Had oatmeal and banana", "CONFIRM_EDIT"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Asks user to send corrected message",
            "Does not save the original",
        ],
        "tags": ["logging", "confirm"],
    },
    {
        "id": 19,
        "category": "Logging & Confirmation",
        "name": "Cancel discards record",
        "messages": ["Had oatmeal and banana", "CONFIRM_CANCEL"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Record is discarded",
            "Confirms nothing was saved",
        ],
        "tags": ["logging", "confirm"],
    },
    {
        "id": 20,
        "category": "Logging & Confirmation",
        "name": "Symptom extraction — nausea",
        "messages": ["Feeling nauseous this morning"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Extracts nausea as a symptom",
            "Shows confirmation with symptom details",
        ],
        "tags": ["logging", "symptom"],
    },
    {
        "id": 21,
        "category": "Logging & Confirmation",
        "name": "Symptom severity captured",
        "messages": ["Nausea 7 out of 10"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Severity of 7 is extracted",
            "Symptom type is nausea",
        ],
        "tags": ["logging", "symptom"],
    },
    {
        "id": 22,
        "category": "Logging & Confirmation",
        "name": "Exercise logged — yoga",
        "messages": ["Did 25 min yoga"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Yoga extracted as activity type",
            "Duration of 25 minutes captured",
        ],
        "tags": ["logging", "exercise"],
    },
    {
        "id": 23,
        "category": "Logging & Confirmation",
        "name": "Medication logged — prenatal vitamin",
        "messages": ["Took prenatal vitamin"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Prenatal vitamin extracted as medication",
            "Confirmation shown",
        ],
        "tags": ["logging", "medication"],
    },
    {
        "id": 24,
        "category": "Logging & Confirmation",
        "name": "Water intake logged",
        "messages": ["Drank 500ml water"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "500ml extracted as water volume",
            "Unit is ml",
        ],
        "tags": ["logging", "water"],
    },
    {
        "id": 25,
        "category": "Logging & Confirmation",
        "name": "Weight logged with unit conversion",
        "messages": ["Current weight is 148 pounds"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Weight of 148 pounds extracted",
            "Unit stored as lbs or converted to kg",
        ],
        "tags": ["logging", "weight"],
    },
    {
        "id": 26,
        "category": "Logging & Confirmation",
        "name": "Doctor question stored",
        "messages": ["Ask my doctor if I can travel in the third trimester"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Question about travel is extracted",
            "Tagged for doctor visit",
        ],
        "tags": ["logging", "question"],
    },
    {
        "id": 27,
        "category": "Logging & Confirmation",
        "name": "Food preference — dislike",
        "messages": ["I don't eat yogurt"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Yogurt extracted as dislike or preference to avoid",
        ],
        "tags": ["logging", "preference"],
    },
    {
        "id": 28,
        "category": "Logging & Confirmation",
        "name": "Allergy saved",
        "messages": ["I'm allergic to peanuts"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Peanut allergy extracted with type 'allergy'",
        ],
        "tags": ["logging", "preference"],
    },
    {
        "id": 29,
        "category": "Logging & Confirmation",
        "name": "Dislike stored",
        "messages": ["I hate avocados"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Avocado extracted as dislike",
        ],
        "tags": ["logging", "preference"],
    },
    {
        "id": 30,
        "category": "Logging & Confirmation",
        "name": "Multi-item breakfast extracted",
        "messages": ["Breakfast was eggs, toast and orange juice"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "At least 3 food items extracted: eggs, toast, orange juice",
            "All items in the meal confirmation",
        ],
        "tags": ["logging", "meal"],
    },
    {
        "id": 31,
        "category": "Logging & Confirmation",
        "name": "Meal timing captured",
        "messages": ["Had lunch at noon"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Meal is extracted even with minimal detail",
            "Lunch timing acknowledged",
        ],
        "tags": ["logging", "meal"],
    },
    {
        "id": 32,
        "category": "Logging & Confirmation",
        "name": "Exercise duration normalized — half hour",
        "messages": ["Walked for half an hour"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Duration extracted as 30 minutes",
            "Activity type is walking or exercise",
        ],
        "tags": ["logging", "exercise"],
    },
    {
        "id": 33,
        "category": "Logging & Confirmation",
        "name": "Medication dosage extracted",
        "messages": ["Took iron tablet 65mg"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Medication name is iron",
            "Dose of 65mg captured",
        ],
        "tags": ["logging", "medication"],
    },
    {
        "id": 34,
        "category": "Logging & Confirmation",
        "name": "Fatigue symptom extracted",
        "messages": ["I feel exhausted today"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Fatigue or exhaustion extracted as symptom",
        ],
        "tags": ["logging", "symptom"],
    },
    {
        "id": 35,
        "category": "Logging & Confirmation",
        "name": "Multiple symptoms in one message",
        "messages": ["Headache and nausea all morning"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "At least headache and nausea are extracted as symptoms",
            "OR single symptom extracted with both mentioned in confirmation",
        ],
        "tags": ["logging", "symptom"],
    },

    # =========================================================================
    # Category 3: Pregnancy Intelligence (36-55)
    # =========================================================================

    {
        "id": 36,
        "category": "Pregnancy Intelligence",
        "name": "Gestational age query — correct weeks",
        "messages": ["How many weeks pregnant am I?"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Returns the correct gestational age in weeks (approximately 8 weeks for LMP 60 days ago)",
            "Response is personalized, not generic",
        ],
        "tags": ["knowledge", "gestational-age"],
    },
    {
        "id": 37,
        "category": "Pregnancy Intelligence",
        "name": "Week-specific milestone",
        "messages": ["What's happening this week with my baby?"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Response mentions week 8 or 9 baby development",
            "Specific developmental details (not generic)",
        ],
        "tags": ["knowledge", "milestone"],
    },
    {
        "id": 38,
        "category": "Pregnancy Intelligence",
        "name": "Baby size comparison",
        "messages": ["How big is my baby right now?"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Gives a size comparison appropriate for ~8-9 weeks (raspberry or kidney bean size)",
            "Response is week-specific",
        ],
        "tags": ["knowledge"],
    },
    {
        "id": 39,
        "category": "Pregnancy Intelligence",
        "name": "Sushi safety — week-aware",
        "messages": ["Can I eat sushi?"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Advises against raw fish during pregnancy",
            "Mentions cooked sushi options as safer",
            "Response is grounded in medical guidance (ACOG/WHO/NHS)",
        ],
        "tags": ["knowledge", "food-safety"],
    },
    {
        "id": 40,
        "category": "Pregnancy Intelligence",
        "name": "Caffeine guidance",
        "messages": ["Can I drink coffee?"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Mentions the recommended caffeine limit (typically 200mg/day per ACOG)",
            "Does not say coffee is completely forbidden",
        ],
        "tags": ["knowledge", "food-safety"],
    },
    {
        "id": 41,
        "category": "Pregnancy Intelligence",
        "name": "Papaya safety",
        "messages": ["Can I eat papaya?"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Distinguishes between ripe and unripe papaya",
            "Warns about unripe/green papaya due to latex",
        ],
        "tags": ["knowledge", "food-safety"],
    },
    {
        "id": 42,
        "category": "Pregnancy Intelligence",
        "name": "Nausea explanation — week-aware",
        "messages": ["Why am I so nauseous?"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Explains morning sickness context appropriate for ~8-9 weeks",
            "Mentions hCG levels or first trimester as cause",
        ],
        "tags": ["knowledge", "symptom"],
    },
    {
        "id": 43,
        "category": "Pregnancy Intelligence",
        "name": "Spotting question — medical escalation if needed",
        "messages": ["Is spotting normal during pregnancy?"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Acknowledges spotting can be normal in early pregnancy",
            "Recommends consulting healthcare provider",
            "Does not dismiss the concern",
        ],
        "tags": ["knowledge", "escalation"],
    },
    {
        "id": 44,
        "category": "Pregnancy Intelligence",
        "name": "Running while pregnant",
        "messages": ["Can I run while pregnant?"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Confirms gentle running is generally safe in first trimester",
            "Advises listening to body and consulting provider",
        ],
        "tags": ["knowledge", "exercise"],
    },
    {
        "id": 45,
        "category": "Pregnancy Intelligence",
        "name": "Baby hearing development",
        "messages": ["Can my baby hear me yet?"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Accurate answer for ~8-9 weeks (ear development not complete at 8 weeks)",
            "Does not falsely claim baby can hear at 8 weeks",
        ],
        "tags": ["knowledge", "development"],
    },
    {
        "id": 46,
        "category": "Pregnancy Intelligence",
        "name": "Yoga safety during pregnancy",
        "messages": ["Is yoga safe during pregnancy?"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Confirms prenatal yoga is generally safe",
            "Mentions poses to avoid (lying flat on back, inversions, etc.)",
        ],
        "tags": ["knowledge", "exercise"],
    },
    {
        "id": 47,
        "category": "Pregnancy Intelligence",
        "name": "Chocolate craving — safe limits",
        "messages": ["I'm craving chocolate constantly"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Dark chocolate in moderation is safe",
            "Mentions caffeine content consideration",
            "Warm, non-judgmental tone",
        ],
        "tags": ["knowledge", "food-safety"],
    },
    {
        "id": 48,
        "category": "Pregnancy Intelligence",
        "name": "Nutrient gaps based on meal history",
        "messages": ["What nutrients am I missing based on what I've been eating?"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [
                {"type": "meal", "items": [{"food_name": "rice", "quantity": 1, "unit": "cup"}]},
            ]
        },
        "expected": [
            "Addresses the question about nutrients",
            "References logged food data OR acknowledges limited history",
            "Suggests pregnancy-important nutrients (iron, folate, calcium, protein)",
        ],
        "tags": ["knowledge", "nutrition", "mixed-query"],
    },
    {
        "id": 49,
        "category": "Pregnancy Intelligence",
        "name": "Dinner suggestion — preference aware",
        "messages": ["Suggest a dinner idea for tonight"],
        "setup": _DEFAULT_MOM | {"food_preference": "vegetarian"},
        "expected": [
            "Suggests a vegetarian dinner option",
            "Does NOT suggest meat dishes",
            "Pregnancy nutrition awareness (iron-rich vegetarian options preferred)",
        ],
        "tags": ["knowledge", "nutrition", "preference"],
    },
    {
        "id": 50,
        "category": "Pregnancy Intelligence",
        "name": "Breakfast suggestion — week aware",
        "messages": ["Suggest something for breakfast"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Suggests foods beneficial for the current trimester",
            "Avoids foods unsafe in pregnancy",
        ],
        "tags": ["knowledge", "nutrition"],
    },
    {
        "id": 51,
        "category": "Pregnancy Intelligence",
        "name": "Tiredness explanation — trimester aware",
        "messages": ["Why am I so tired all day?"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Explains first trimester fatigue",
            "Mentions hormonal changes (progesterone) or increased blood supply",
            "Offers practical suggestions",
        ],
        "tags": ["knowledge"],
    },
    {
        "id": 52,
        "category": "Pregnancy Intelligence",
        "name": "Ibuprofen safety",
        "messages": ["Can I take ibuprofen for a headache?"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Advises against ibuprofen (NSAIDs) during pregnancy",
            "Suggests paracetamol/acetaminophen as safer alternative",
            "Recommends consulting provider",
        ],
        "tags": ["knowledge", "medication"],
    },
    {
        "id": 53,
        "category": "Pregnancy Intelligence",
        "name": "Pineapple safety",
        "messages": ["Is it safe to eat pineapple during pregnancy?"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Addresses bromelain concern",
            "Confirms pineapple in normal amounts is safe",
            "Does not sensationalize",
        ],
        "tags": ["knowledge", "food-safety"],
    },
    {
        "id": 54,
        "category": "Pregnancy Intelligence",
        "name": "Daily water intake — personalized",
        "messages": ["How much water should I drink each day?"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Gives specific guidance (approx 2-3 litres/day during pregnancy)",
            "Mentions pregnancy increases fluid needs",
        ],
        "tags": ["knowledge"],
    },
    {
        "id": 55,
        "category": "Pregnancy Intelligence",
        "name": "Sleeping position — week-aware",
        "messages": ["Should I sleep on my back?"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "At 8-9 weeks back sleeping is generally still fine",
            "Mentions that later in pregnancy (after 20 weeks) left side is recommended",
        ],
        "tags": ["knowledge"],
    },

    # =========================================================================
    # Category 4: Memory & Personalization (56-75)
    # =========================================================================

    {
        "id": 56,
        "category": "Memory & Personalization",
        "name": "Retrieve yesterday's meals",
        "messages": ["What did I eat yesterday?"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [{"type": "meal", "days_ago": 1, "items": [{"food_name": "dal", "quantity": 1, "unit": "bowl"}]}]
        },
        "expected": [
            "Returns meal(s) logged yesterday",
            "Mentions dal or the specific logged item",
        ],
        "tags": ["query", "meal"],
    },
    {
        "id": 57,
        "category": "Memory & Personalization",
        "name": "Symptom summary this week",
        "messages": ["Show my symptoms from this week"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [{"type": "symptom", "days_ago": 2, "symptom_name": "nausea", "severity": 5, "frequency": 2}]
        },
        "expected": [
            "Returns the nausea symptom logged this week",
            "Includes severity or frequency details",
        ],
        "tags": ["query", "symptom"],
    },
    {
        "id": 58,
        "category": "Memory & Personalization",
        "name": "Count headache occurrences",
        "messages": ["How many headaches have I had this month?"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [
                {"type": "symptom", "days_ago": 3, "symptom_name": "headache", "severity": 4, "frequency": 1},
                {"type": "symptom", "days_ago": 7, "symptom_name": "headache", "severity": 3, "frequency": 1},
            ]
        },
        "expected": [
            "Returns or mentions the headache records",
            "Gives count or summary of 2 headaches",
        ],
        "tags": ["query", "symptom"],
    },
    {
        "id": 59,
        "category": "Memory & Personalization",
        "name": "Retrieve saved doctor questions",
        "messages": ["What questions have I saved for my doctor?"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [{"type": "question", "days_ago": 5, "question_text": "Is it safe to travel in the third trimester?"}]
        },
        "expected": [
            "Returns the saved doctor question about travel",
        ],
        "tags": ["query", "question"],
    },
    {
        "id": 60,
        "category": "Memory & Personalization",
        "name": "Exercise history retrieval",
        "messages": ["What exercises did I do this week?"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [{"type": "exercise", "days_ago": 1, "activity_type": "yoga", "duration_minutes": 30}]
        },
        "expected": [
            "Returns yoga exercise logged this week",
            "Duration of 30 minutes mentioned",
        ],
        "tags": ["query", "exercise"],
    },
    {
        "id": 61,
        "category": "Memory & Personalization",
        "name": "Medication list retrieval",
        "messages": ["What medications am I currently taking?"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [{"type": "medication", "days_ago": 1, "medication_name": "iron", "dose": "65mg"}]
        },
        "expected": [
            "Returns iron medication",
            "Dose of 65mg included",
        ],
        "tags": ["query", "medication"],
    },
    {
        "id": 62,
        "category": "Memory & Personalization",
        "name": "Weight trend summary",
        "messages": ["What is my weight trend over the past week?"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [
                {"type": "weight", "days_ago": 7, "value": 67.0, "unit": "kg"},
                {"type": "weight", "days_ago": 3, "value": 67.5, "unit": "kg"},
            ]
        },
        "expected": [
            "Returns weight data",
            "Describes a slight increase or trend",
        ],
        "tags": ["query", "weight"],
    },
    {
        "id": 63,
        "category": "Memory & Personalization",
        "name": "Water intake summary",
        "messages": ["How much water have I been drinking?"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [
                {"type": "water", "days_ago": 1, "volume": 1500, "unit": "ml"},
                {"type": "water", "days_ago": 2, "volume": 2000, "unit": "ml"},
            ]
        },
        "expected": [
            "Returns water intake data",
            "Mentions volumes logged",
        ],
        "tags": ["query", "water"],
    },
    {
        "id": 64,
        "category": "Memory & Personalization",
        "name": "Recall food preferences",
        "messages": ["What foods do I avoid?"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [{"type": "preference", "preference_type": "dislike", "food_item": "yogurt"}]
        },
        "expected": [
            "Returns yogurt as a food to avoid",
        ],
        "tags": ["query", "preference"],
    },
    {
        "id": 65,
        "category": "Memory & Personalization",
        "name": "Snack suggestion avoids rejected food",
        "messages": ["Suggest a snack"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [{"type": "preference", "preference_type": "dislike", "food_item": "yogurt"}]
        },
        "expected": [
            "Does NOT suggest yogurt",
            "Suggests a different healthy snack",
        ],
        "tags": ["knowledge", "preference", "personalization"],
    },
    {
        "id": 66,
        "category": "Memory & Personalization",
        "name": "Breakfast suggestion is vegetarian",
        "messages": ["Suggest something for breakfast"],
        "setup": _DEFAULT_MOM | {"food_preference": "vegetarian"},
        "expected": [
            "Suggests a vegetarian breakfast",
            "No meat or fish items",
        ],
        "tags": ["knowledge", "preference"],
    },
    {
        "id": 67,
        "category": "Memory & Personalization",
        "name": "Preference update — no more yogurt",
        "messages": ["I don't want yogurt suggestions anymore"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Extracts yogurt as a dislike or preference to avoid",
            "Confirms it will be remembered",
        ],
        "tags": ["logging", "preference"],
    },
    {
        "id": 68,
        "category": "Memory & Personalization",
        "name": "Doctor questions retrieval",
        "messages": ["What did I ask my doctor last month?"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [{"type": "question", "days_ago": 20, "question_text": "Can I travel after week 32?"}]
        },
        "expected": [
            "Returns the travel question saved last month",
        ],
        "tags": ["query", "question"],
    },
    {
        "id": 69,
        "category": "Memory & Personalization",
        "name": "Exercise frequency calculation",
        "messages": ["How often have I been exercising this week?"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [
                {"type": "exercise", "days_ago": 1, "activity_type": "yoga", "duration_minutes": 30},
                {"type": "exercise", "days_ago": 3, "activity_type": "walking", "duration_minutes": 20},
            ]
        },
        "expected": [
            "Returns 2 exercise sessions this week",
            "OR summarizes frequency appropriately",
        ],
        "tags": ["query", "exercise"],
    },
    {
        "id": 70,
        "category": "Memory & Personalization",
        "name": "Iron intake assessment — mixed query",
        "messages": ["Am I eating enough iron?"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [{"type": "meal", "days_ago": 1, "items": [{"food_name": "spinach", "quantity": 1, "unit": "cup"}]}]
        },
        "expected": [
            "References meal history OR acknowledges limited data",
            "Provides iron RDI context for pregnancy",
            "Suggests iron-rich foods",
        ],
        "tags": ["knowledge", "nutrition", "mixed-query"],
    },
    {
        "id": 71,
        "category": "Memory & Personalization",
        "name": "Headache cause — mixed query with history",
        "messages": ["Why am I getting headaches?"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [
                {"type": "symptom", "days_ago": 2, "symptom_name": "headache", "severity": 5, "frequency": 2},
                {"type": "water", "days_ago": 2, "volume": 500, "unit": "ml"},
            ]
        },
        "expected": [
            "References symptom history",
            "Considers possible causes (dehydration, hormones, etc.)",
            "Practical suggestions provided",
        ],
        "tags": ["mixed-query"],
    },
    {
        "id": 72,
        "category": "Memory & Personalization",
        "name": "Diet causing fatigue — mixed query",
        "messages": ["Could my diet be causing my fatigue?"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [
                {"type": "meal", "days_ago": 1, "items": [{"food_name": "rice", "quantity": 1, "unit": "cup"}]},
                {"type": "symptom", "days_ago": 1, "symptom_name": "fatigue", "severity": 6, "frequency": 1},
            ]
        },
        "expected": [
            "Connects diet to fatigue possibility",
            "Suggests nutritional considerations (iron, protein)",
            "Uses both personal data and medical knowledge",
        ],
        "tags": ["mixed-query"],
    },
    {
        "id": 73,
        "category": "Memory & Personalization",
        "name": "Previous nausea history lookup",
        "messages": ["Have I reported nausea before?"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [{"type": "symptom", "days_ago": 10, "symptom_name": "nausea", "severity": 4, "frequency": 2}]
        },
        "expected": [
            "Confirms nausea was previously logged",
            "Mentions when or frequency",
        ],
        "tags": ["query", "symptom"],
    },
    {
        "id": 74,
        "category": "Memory & Personalization",
        "name": "Week over week comparison",
        "messages": ["Compare my symptoms this week to last week"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [
                {"type": "symptom", "days_ago": 2, "symptom_name": "nausea", "severity": 3, "frequency": 1},
                {"type": "symptom", "days_ago": 9, "symptom_name": "nausea", "severity": 6, "frequency": 3},
            ]
        },
        "expected": [
            "Compares symptoms across two time windows",
            "Notes improvement or change in severity/frequency",
        ],
        "tags": ["query", "symptom"],
    },
    {
        "id": 75,
        "category": "Memory & Personalization",
        "name": "Personalized weekly focus",
        "messages": ["What should I focus on this week?"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Provides week-specific advice (~8-9 weeks)",
            "Mentions relevant topics for first trimester",
            "Personalized, not generic",
        ],
        "tags": ["knowledge"],
    },

    # =========================================================================
    # Category 5: Appointments & Doctor Workflow (76-90)
    # =========================================================================

    {
        "id": 76,
        "category": "Appointments & Doctor Workflow",
        "name": "OB visit — natural language scheduling",
        "messages": ["My OB visit is June 30 at 10am"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Appointment extracted with type ob_visit",
            "Date and time June 30 at 10:00 captured",
            "Confirmation shown",
        ],
        "tags": ["appointment", "logging"],
    },
    {
        "id": 77,
        "category": "Appointments & Doctor Workflow",
        "name": "List upcoming appointments",
        "messages": ["What appointments do I have coming up?"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [{"type": "appointment", "days_from_now": 7, "appointment_type": "ultrasound"}]
        },
        "expected": [
            "Returns the ultrasound appointment",
            "Shows date and type",
        ],
        "tags": ["query", "appointment"],
    },
    {
        "id": 78,
        "category": "Appointments & Doctor Workflow",
        "name": "Cancel appointment via command",
        "messages": ["/appointments"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [{"type": "appointment", "days_from_now": 5, "appointment_type": "ultrasound"}]
        },
        "expected": [
            "Shows appointment management menu",
            "Cancel option available",
        ],
        "tags": ["appointment"],
    },
    {
        "id": 79,
        "category": "Appointments & Doctor Workflow",
        "name": "Reschedule bloodwork",
        "messages": ["/appointments"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [{"type": "appointment", "days_from_now": 3, "appointment_type": "bloodwork"}]
        },
        "expected": [
            "Shows appointment management menu",
            "Reschedule option available",
        ],
        "tags": ["appointment"],
    },
    {
        "id": 80,
        "category": "Appointments & Doctor Workflow",
        "name": "Doctor question — include in summary",
        "messages": ["Ask doctor about magnesium supplements"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Doctor question about magnesium is extracted",
            "Marked as doctor_visit_tagged",
        ],
        "tags": ["logging", "question"],
    },
    {
        "id": 81,
        "category": "Appointments & Doctor Workflow",
        "name": "Doctor question — travel safety",
        "messages": ["Ask doctor if travel is safe at this stage"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Doctor question about travel extracted",
            "Stored for doctor visit",
        ],
        "tags": ["logging", "question"],
    },
    {
        "id": 82,
        "category": "Appointments & Doctor Workflow",
        "name": "Retrieve saved doctor questions",
        "messages": ["Show me the questions I've saved for my doctor"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [
                {"type": "question", "days_ago": 5, "question_text": "Is magnesium safe?"},
                {"type": "question", "days_ago": 3, "question_text": "Can I travel?"},
            ]
        },
        "expected": [
            "Returns both saved questions",
            "Mentions magnesium and travel",
        ],
        "tags": ["query", "question"],
    },
    {
        "id": 83,
        "category": "Appointments & Doctor Workflow",
        "name": "Anatomy scan appointment creation",
        "messages": ["My anatomy scan is July 15 at 2pm"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Appointment extracted as ultrasound type",
            "July 15 at 14:00 captured",
        ],
        "tags": ["appointment", "logging"],
    },
    {
        "id": 84,
        "category": "Appointments & Doctor Workflow",
        "name": "Symptoms to mention at next visit",
        "messages": ["What symptoms should I mention at my next doctor visit?"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [
                {"type": "symptom", "days_ago": 3, "symptom_name": "headache", "severity": 7, "frequency": 3},
                {"type": "symptom", "days_ago": 5, "symptom_name": "nausea", "severity": 5, "frequency": 2},
            ]
        },
        "expected": [
            "References logged symptoms",
            "Highlights high-severity or frequent symptoms",
            "Practical suggestion to mention them",
        ],
        "tags": ["mixed-query", "symptom"],
    },
    {
        "id": 85,
        "category": "Appointments & Doctor Workflow",
        "name": "Auto-reminders created with appointment",
        "messages": ["My scan is next Tuesday at 11am", "CONFIRM_SAVE"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Appointment saved",
            "Confirms that 24h and 1h reminders are set",
        ],
        "tags": ["appointment", "reminder"],
    },
    {
        "id": 86,
        "category": "Appointments & Doctor Workflow",
        "name": "What happened since last visit",
        "messages": ["What has happened health-wise in the last two weeks?"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [
                {"type": "symptom", "days_ago": 5, "symptom_name": "nausea", "severity": 4, "frequency": 2},
                {"type": "meal", "days_ago": 3, "items": [{"food_name": "spinach", "quantity": 1, "unit": "cup"}]},
            ]
        },
        "expected": [
            "Summarizes recent health logs",
            "Mentions symptoms and/or meals from the period",
        ],
        "tags": ["query"],
    },
    {
        "id": 87,
        "category": "Appointments & Doctor Workflow",
        "name": "Weight trend since last visit",
        "messages": ["Show my weight trend over the last two weeks"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [
                {"type": "weight", "days_ago": 14, "value": 66.5, "unit": "kg"},
                {"type": "weight", "days_ago": 7, "value": 67.0, "unit": "kg"},
                {"type": "weight", "days_ago": 2, "value": 67.5, "unit": "kg"},
            ]
        },
        "expected": [
            "Shows weight measurements over two weeks",
            "Notes gradual increase",
        ],
        "tags": ["query", "weight"],
    },
    {
        "id": 88,
        "category": "Appointments & Doctor Workflow",
        "name": "Medication changes summary",
        "messages": ["What medications have I been taking this week?"],
        "setup": _DEFAULT_MOM | {
            "pre_log": [
                {"type": "medication", "days_ago": 3, "medication_name": "iron", "dose": "65mg"},
                {"type": "medication", "days_ago": 5, "medication_name": "folic acid", "dose": "400mcg"},
            ]
        },
        "expected": [
            "Returns iron and folic acid",
            "Doses included",
        ],
        "tags": ["query", "medication"],
    },
    {
        "id": 89,
        "category": "Appointments & Doctor Workflow",
        "name": "Reminder — natural language",
        "messages": ["Remind me to take my prenatal vitamin every morning at 8am"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Reminder extracted with type vitamin",
            "Time 08:00 captured",
            "Recurring nature acknowledged",
        ],
        "tags": ["reminder", "logging"],
    },
    {
        "id": 90,
        "category": "Appointments & Doctor Workflow",
        "name": "Reminder saved and confirmed",
        "messages": ["Remind me to drink water every 2 hours", "CONFIRM_SAVE"],
        "setup": _DEFAULT_MOM,
        "expected": [
            "Water reminder saved",
            "Confirmation shows reminder set",
        ],
        "tags": ["reminder", "confirm"],
    },

    # =========================================================================
    # Category 6: Family & Privacy (91-100)
    # THESE ARE THE MOST IMPORTANT — they validate the privacy moat
    # =========================================================================

    {
        "id": 91,
        "category": "Family & Privacy",
        "name": "Private symptom NOT visible to partner",
        "messages": ["How is my partner doing?"],
        "setup": _DEFAULT_PARTNER | {
            "partner_pre_log": [
                {"type": "symptom", "visibility": "private", "symptom_name": "nausea", "severity": 6, "frequency": 2}
            ]
        },
        "expected": [
            "Does NOT reveal the private nausea symptom",
            "Either says no shared data or answers generally",
        ],
        "tags": ["privacy", "visibility", "partner"],
    },
    {
        "id": 92,
        "category": "Family & Privacy",
        "name": "Shared question visible to partner",
        "messages": ["What questions do we have for the doctor?"],
        "setup": _DEFAULT_PARTNER | {
            "partner_pre_log": [
                {"type": "question", "visibility": "partner_shared", "question_text": "Is travel safe after 32 weeks?"}
            ]
        },
        "expected": [
            "Returns the shared travel question",
            "Correctly surfaces partner-shared data",
        ],
        "tags": ["privacy", "visibility", "partner"],
    },
    {
        "id": 93,
        "category": "Family & Privacy",
        "name": "Shared appointment visible to partner",
        "messages": ["When is our next appointment?"],
        "setup": _DEFAULT_PARTNER | {
            "partner_pre_log": [
                {"type": "appointment", "visibility": "partner_shared", "days_from_now": 7, "appointment_type": "ultrasound"}
            ]
        },
        "expected": [
            "Returns the shared ultrasound appointment",
            "Date is correct",
        ],
        "tags": ["privacy", "visibility", "partner"],
    },
    {
        "id": 94,
        "category": "Family & Privacy",
        "name": "Only shared symptoms visible — not private ones",
        "messages": ["Any symptoms this week?"],
        "setup": _DEFAULT_PARTNER | {
            "partner_pre_log": [
                {"type": "symptom", "visibility": "private", "symptom_name": "back pain", "severity": 4, "frequency": 1},
                {"type": "symptom", "visibility": "partner_shared", "symptom_name": "nausea", "severity": 5, "frequency": 2},
            ]
        },
        "expected": [
            "Returns nausea (partner_shared)",
            "Does NOT return back pain (private)",
        ],
        "tags": ["privacy", "visibility", "partner"],
    },
    {
        "id": 95,
        "category": "Family & Privacy",
        "name": "Partner-specific support guidance",
        "messages": ["How can I help my partner this week?"],
        "setup": _DEFAULT_PARTNER,
        "expected": [
            "Addresses the question from a partner/dad perspective",
            "Does NOT address the user as if they are the pregnant person",
            "Week-specific support advice (~8-9 weeks)",
        ],
        "tags": ["knowledge", "partner", "role-aware"],
    },
    {
        "id": 96,
        "category": "Family & Privacy",
        "name": "Baby development — dad framing",
        "messages": ["What's happening with our baby this week?"],
        "setup": _DEFAULT_PARTNER,
        "expected": [
            "Frames response for the partner/dad perspective",
            "Week-specific baby development details",
            "Warm, inclusive language",
        ],
        "tags": ["knowledge", "partner", "role-aware"],
    },
    {
        "id": 97,
        "category": "Family & Privacy",
        "name": "Partner cannot access private symptom directly",
        "messages": ["Show me her back pain symptoms"],
        "setup": _DEFAULT_PARTNER | {
            "partner_pre_log": [
                {"type": "symptom", "visibility": "private", "symptom_name": "back pain", "severity": 5, "frequency": 1}
            ]
        },
        "expected": [
            "Does NOT return private back pain symptom",
            "Responds with no data available or access denied",
        ],
        "tags": ["privacy", "visibility", "partner"],
    },
    {
        "id": 98,
        "category": "Family & Privacy",
        "name": "Visibility change respected in future queries",
        "messages": ["What symptoms does my partner have?"],
        "setup": _DEFAULT_PARTNER | {
            "partner_pre_log": [
                {"type": "symptom", "visibility": "partner_shared", "symptom_name": "fatigue", "severity": 5, "frequency": 3}
            ]
        },
        "expected": [
            "Returns the partner_shared fatigue symptom",
        ],
        "tags": ["privacy", "visibility", "partner"],
    },
    {
        "id": 99,
        "category": "Family & Privacy",
        "name": "Partner product suggestions — pregnancy stage aware",
        "messages": ["What should I buy or prepare this month?"],
        "setup": _DEFAULT_PARTNER,
        "expected": [
            "Gives stage-relevant suggestions (first trimester items)",
            "Framed for the partner/dad doing the preparation",
        ],
        "tags": ["knowledge", "partner"],
    },
    {
        "id": 100,
        "category": "Family & Privacy",
        "name": "Pre-appointment summary — shared data only",
        "messages": ["Give me a summary of what I should know before our appointment"],
        "setup": _DEFAULT_PARTNER | {
            "partner_pre_log": [
                {"type": "symptom", "visibility": "private", "symptom_name": "anxiety", "severity": 4, "frequency": 2},
                {"type": "symptom", "visibility": "partner_shared", "symptom_name": "nausea", "severity": 5, "frequency": 3},
                {"type": "question", "visibility": "partner_shared", "question_text": "Can she fly at 28 weeks?"},
            ]
        },
        "expected": [
            "Summary includes nausea (partner_shared) and the travel question (partner_shared)",
            "Does NOT include anxiety (private)",
            "Useful pre-appointment briefing for the partner",
        ],
        "tags": ["privacy", "visibility", "partner", "mixed-query"],
    },
]
