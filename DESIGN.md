# Famosi — Design & Feature Reference

> Last updated: June 2026  
> Status: Live on AWS (EC2 `i-0a60550ccc4867f40`, `44.211.89.114`)

---

## 1. What is Famosi?

Famosi is an AI-powered pregnancy companion delivered through Telegram. Users interact entirely in natural language — no slash commands required, no forms to fill. The bot understands what the user wants, acts on it, and responds conversationally.

**Core principle:** The model handles intent. Tools handle persistence. The user just talks.

---

## 2. Architecture Overview

```
Telegram (polling)
       │
       ▼
AuthMiddleware (group -1)
  └─ Loads User from DB, sets current_user, checks approval/subscription
       │
       ▼
ConversationHandlers (group 0) ─── onboarding, consent, /appointments, /reminders
       │ (if not consumed)
       ▼
Dispatcher catch-all (group 1)
  └─ IntentRouter → GPT-4.1-nano classifies intent
       │
       ├── LOGGING          → LoggingHandler → Extractor → Confirm → Persist
       ├── PERSONAL_DATA_QUERY → QueryHandler → DB fetch → Mini LLM format
       ├── KNOWLEDGE_QUESTION → KnowledgeHandler → RAG → Reasoning LLM
       ├── MIXED_QUERY       → both query + knowledge concurrently → Reasoning synthesis
       └── UNCLASSIFIED      → error message, no pipeline invoked
```

### LLM tiers

| Tier | Model | Provider | Used for |
|---|---|---|---|
| `nano` | gpt-4.1-nano | OpenAI | Intent classification, data extraction |
| `mini` | gpt-4.1-mini | OpenAI | Personal data query formatting, daily summaries |
| `reasoning` | Claude Sonnet 4.5 | AWS Bedrock | Medical Q&A, knowledge synthesis, milestones |
| `escalation` | Claude Sonnet 4.5 | AWS Bedrock | Danger keywords, low confidence, high severity |

Bedrock credentials come from the EC2 IAM role — no keys needed in `.env`.

### Infrastructure (AWS, us-east-1)

| Resource | Details |
|---|---|
| EC2 | t3.micro, Amazon Linux 2023, 20GB gp3 EBS |
| PostgreSQL | 16 + pgvector, installed locally on the EC2 |
| S3 | `famosi-summaries-441870953351` (PDF exports), `famosi-backups-441870953351` (nightly pg_dump) |
| SSM | All secrets stored under `/famosi/*` |
| CloudWatch | Log group `/famosi/app`, CPU + status alarms |
| CDK | Stack defined in `infra/lib/famosi-stack.ts` |

---

## 3. User Roles

| Role | Description |
|---|---|
| **Mom** | Primary user. Full read/write on all her health data. Gets pregnancy milestones, daily facts. |
| **Partner** | Linked to mom's account via invite code. Gets partner-specific content (dad_support RAG category). Sees shared health records based on mom's visibility settings. |
| **Admin** | Identified by `ADMIN_TELEGRAM_USER_ID` env var. Full admin panel via Telegram commands. |

### Family linking

1. Either mom or partner runs `/invite` → bot generates a 6-character code and creates a `FamilyUnit` row.
2. The other person enters the code during onboarding → both users are linked to the same `family_unit_id`.
3. `/invite` can be used by either role. Creator cannot join their own code.

---

## 4. Onboarding

Multi-step ConversationHandler with Redis partial-state persistence (24h TTL, falls back to in-process dict).

**Mom flow:** ROLE → INVITE_CODE → DUE_DATE → COUNTRY → TIMEZONE → LANGUAGE → FIRST_PREGNANCY → FOOD_PREFERENCE → EXERCISE_HABIT → WAKE_TIME → SLEEP_TIME

**Partner flow:** ROLE → INVITE_CODE → LMP_DATE → COUNTRY → TIMEZONE → LANGUAGE → FIRST_PREGNANCY → FOOD_PREFERENCE → WAKE_TIME → SLEEP_TIME → SUPPORT_PREFS → SHARED_TIMELINE

After onboarding completes:
- User is marked `approval_pending = True` in the in-process admin registry.
- Admin receives a Telegram notification with `/approve <telegram_user_id>` and `/reject` links.
- User sees "awaiting approval" until admin approves.
- On approval: 7-day trial activated via `PaymentStateMachine.activate_trial()`.

**`/start` re-onboarding:** Prompts with a yes/no confirmation. "Yes" hard-deletes the User row (all health data cascades), clears pending state, restarts.

---

## 5. Features

### 5.1 Health Logging (LOGGING intent)

User speaks naturally. The extractor (GPT-4.1-nano) parses the message into a typed Pydantic schema. User confirms once. Data is saved.

| What user says | Record type | Schema |
|---|---|---|
| "I had oatmeal and banana for breakfast" | `meal` | `MealExtraction` |
| "Feeling nauseous, severity 4 out of 10" | `symptom` | `SymptomExtraction` |
| "Did 30 minutes of yoga" | `exercise` | `ExerciseExtraction` |
| "Took iron tablet 65mg" | `medication` | `MedicationExtraction` |
| "I weigh 68kg" | `weight` | `WeightExtraction` |
| "Drank 500ml of water" | `water` | `WaterExtraction` |
| "I want to ask my doctor about the anatomy scan" | `question` | `DoctorQuestionExtraction` |
| "I'm vegetarian" | `preference` | `PreferenceExtraction` |
| "My scan is tomorrow at 10:30am" | `appointment` | `AppointmentExtraction` |
| "Remind me to take my vitamin at 9am" | `reminder` | `ReminderExtraction` |

**Confirmation flow:**
1. Summary shown with Save / Edit / Cancel keyboard.
2. Save → persists via `personal_memory` or `appointment_tracker` / `reminder_system`.
3. For health records: visibility prompt offered (Private / Partner Shared / Doctor Shared).
4. For appointments: "Saved + reminders set for 24h and 1h before 🔔".
5. Edit → up to 3 rounds; then discarded.

**Extractor:** `app/core/extractor.py` — single function `extract(record_type, message, llm_client)`. All Pydantic schemas are in `app/schemas/`. Today's date is injected into the system prompt so relative dates ("tomorrow", "next Friday") resolve correctly.

### 5.2 Personal Data Queries (PERSONAL_DATA_QUERY intent)

User asks about their own data. The query handler:
1. Nano LLM extracts `record_type` + optional `date_range`.
2. Fetches from the right store (personal_memory for health records; appointment_tracker / reminder_system for scheduling data).
3. Mini LLM formats results into a natural reply.

Supported record types for querying: `meal`, `symptom`, `exercise`, `medication`, `weight_log`, `water_log`, `doctor_question`, `preference`, **`appointment`**, **`reminder`**.

Partner visibility is enforced at the DB query layer — partners only see `partner_shared` and `doctor_shared` records.

**Examples:**
- *"What did I eat yesterday?"* → fetches meals from yesterday
- *"Show me my symptoms this week"* → fetches symptoms with 7-day window
- *"What appointments do I have coming up?"* → fetches all future non-cancelled appointments
- *"What reminders are set?"* → lists active reminders

If the query doesn't map to a record type, it falls through to the knowledge handler (which answers from LLM with gestational context).

### 5.3 Knowledge Q&A (KNOWLEDGE_QUESTION intent)

RAG pipeline:
1. Embed the query with `text-embedding-3-small`.
2. pgvector cosine similarity search on `knowledge_chunks` — top 5 chunks.
3. For partner role: first searches `dad_support` category, falls back to general on empty.
4. System prompt includes both the RAG chunks AND the user's gestational context (weeks pregnant, due date).
5. Reasoning tier (Claude) composes the answer.
6. If RAG returns nothing: falls back to LLM-only with gestational context.

Gestational context is always injected — the model knows exactly how far along the pregnancy is and frames answers accordingly. Partner answers are framed from the partner's perspective ("how to support her") not the mom's.

**Knowledge base categories:** nutrition, symptoms, exercise, medications, baby_development, labor, postpartum, mental_health, dad_support

Sources: ACOG, WHO, CDC, NHS.

**Escalation triggers** (routes to `escalation` tier regardless of intent):
- Message contains: `bleeding`, `preeclampsia`, `contractions`, `seizure`, `chest pain`
- Classification confidence < 0.6
- KNOWLEDGE_QUESTION with symptom severity ≥ 9

### 5.4 Mixed Queries (MIXED_QUERY intent)

Both the personal data path and knowledge path run concurrently (`asyncio.gather`). Results are synthesised by the reasoning tier.

Partial failure policy:
- One path fails → use the other with a note
- Both fail → generic error, no synthesis attempted

### 5.5 Appointments

Natural language creation:
> *"My OB visit is June 25 at 11am"* → extracted, confirmed, saved, 24h+1h reminders auto-created.

Types: `ob_visit`, `ultrasound`, `bloodwork`

Management (via `/appointments` command for list/cancel/reschedule — the ConversationHandler is still available for guided flows).

Auto-reminders: `appointment_tracker.create_appointment` calls `reminder_system.create_appointment_reminders` which creates two `Reminder` rows: `appointment_at - 24h` and `appointment_at - 1h`.

### 5.6 Reminders

Natural language creation:
> *"Remind me to drink water every 2 hours"*  
> *"Set a reminder for my prenatal vitamin at 9am daily"*

Types: `vitamin`, `meal`, `water`, `exercise`, `appointment`

Time is stored as UTC. The user's timezone (stored during onboarding) is used to convert local time → UTC via `reminder_system._localtime_to_utc`.

Delivery: a scheduled job (`jobs/reminder_job.py`) polls every minute for due reminders, sends via Telegram, marks as delivered.

Management: `/reminders` command for list/delete/reschedule.

### 5.7 Nutrition Tracking

`nutrition_assistant.py` — tracks protein, iron, calcium, folate, fiber against pregnancy RDIs.
- Deficiency alert when < 75% RDI for 3 consecutive days.
- Weekly trend digest.
- Meal suggestions grounded in RAG.

### 5.8 Symptom Tracking

`symptom_assistant.py` — logs severity (1–10) and frequency.
- 90-day trend reports.
- Severity ≥ 8 triggers a note to consult healthcare provider.
- Knowledge-grounded guidance.

### 5.9 Weekly Milestones & Daily Facts

`pregnancy_engine.py`:
- Gestational age computed from due date or LMP: `weeks = (280 - days_until_due) // 7`
- Daily fact: delivered once per UTC day (idempotent via `last_daily_fact_date`).
- Weekly milestone: delivered once per gestational week (idempotent via `last_milestone_week`).
- Partner gets dad_support content; mom gets baby_development content.
- Both grounded in RAG when available, fall back to LLM-only.

### 5.10 Doctor Visit Prep

`doctor_visit_assistant.py` — generates a pre-appointment summary:
- Logged symptoms since last appointment
- Saved doctor questions
- Medication changes
- Weight trend
- Exported as PDF via WeasyPrint, uploaded to S3.

### 5.11 Consent

After onboarding completes, the Privacy Policy is automatically presented. The user taps "I Accept" — a `ConsentRecord` row is written with `policy_version`. On policy version change, users are re-prompted before any new health data writes.

### 5.12 Subscription / Payment

`PaymentStateMachine` manages the lifecycle:

```
new_user → TRIAL (7 days) → GRACE (48h read-only) → INACTIVE
                          ↓ payment success
                        ACTIVE → INACTIVE (on expiry or 3 failed retries)
```

Payment providers: Razorpay (India), Stripe (USA). Other regions: unsupported message.

Renewal reminders sent daily within 7 days of expiry.

---

## 6. Privacy Architecture

- **Structlog processor** strips ~20+ sensitive field names from every log event before emission: message text, food names, symptom names, health values, etc.
- Health data is never logged — only structural/operational fields (user_id integer, intent labels, token counts, latency).
- Visibility levels on all health records: `private` (default), `partner_shared`, `doctor_shared`.
- Partner DB queries filtered at query layer — never in Python.
- Daily token cap per user: 100,000 tokens/day. Exceeded users get `mini` tier until midnight UTC.

---

## 7. Admin Panel

Activated by setting `ADMIN_TELEGRAM_USER_ID` in `.env`.

| Command | Action |
|---|---|
| `/admin` | Show help menu |
| `/stats` | DB snapshot: users, subscriptions, total requests |
| `/metrics` | 24h rolling digest: LLM cost, token usage, latency p50/p95/p99, intent breakdown |
| `/pending` | List users awaiting approval |
| `/approve <id>` | Approve user, activate 7-day trial, notify them |
| `/reject <id>` | Remove user record, notify them |
| `/users` | Last 20 users with status |
| `/user <id>` | Detail view for one user |

Daily digest sent at 02:00 UTC automatically.

---

## 8. Scheduled Jobs

All triggered via AWS EventBridge → HTTP POST to job endpoints (authenticated with `JOB_SECRET`).

| Job | Schedule | What it does |
|---|---|---|
| `reminder_job` | Every minute | Deliver due reminders, retry failed once |
| `pregnancy_update_job` | Daily | Daily facts + weekly milestones |
| `weekly_report_job` | Weekly | Nutrition + symptom digest |
| `backup_job` | Daily (02:00 UTC) | pg_dump → gzip → S3 |

---

## 9. Database Schema

Key tables and their purpose:

| Table | Purpose |
|---|---|
| `users` | Core user record: role, due_date, lmp_date, timezone, preferences |
| `family_units` | Links mom + partner; stores invite_code |
| `subscriptions` | Subscription state machine state per user |
| `consent_records` | Privacy policy acceptance history |
| `meals` + `meal_items` + `meal_nutrients` | Food logging |
| `symptoms` | Symptom logging with severity/frequency |
| `exercises` | Exercise logging |
| `medications` | Medication logging |
| `weight_logs` | Weight measurements |
| `water_logs` | Water intake |
| `doctor_questions` | Questions saved for appointments |
| `preferences` | Food preferences/allergies |
| `appointments` | Medical appointments with auto-reminder links |
| `reminders` | Scheduled reminders (all types) |
| `knowledge_documents` | RAG source documents |
| `knowledge_chunks` | Chunked + embedded text (vector column, 1536 dims) |
| `request_logs` | Every dispatch call: intent, model, tokens, latency |

Migrations managed by Alembic. Current: `0002` (invite_code on family_units).

---

## 10. Code Structure

```
app/
├── main.py                  # FastAPI app + PTB polling + handler registration
├── config.py                # Settings (pydantic-settings, reads .env)
├── dependencies.py          # DB engine + session factory + Redis
│
├── core/
│   ├── intent_router.py     # 4-way intent classification + escalation
│   ├── llm_client.py        # Unified LLM client (OpenAI + Bedrock)
│   ├── extractor.py         # LLM extraction pipeline (all record types)
│   ├── confirmation.py      # ConfirmationSession + ConfirmationStore
│   └── request_logger.py    # DB request log writer
│
├── bot/
│   ├── dispatcher.py        # Central routing: intent → handler
│   ├── middleware/
│   │   ├── auth.py          # User loading, subscription check, token cap
│   │   └── request_id.py    # Request ID injection for log correlation
│   ├── handlers/
│   │   ├── onboarding.py    # Multi-step onboarding + /invite + reset
│   │   ├── consent.py       # Privacy policy flow
│   │   ├── logging_handler.py  # LOGGING: extraction → confirm → persist
│   │   ├── query_handler.py    # PERSONAL_DATA_QUERY
│   │   ├── knowledge_handler.py # KNOWLEDGE_QUESTION + role-aware RAG
│   │   ├── appointment_handler.py # /appointments management ConversationHandler
│   │   ├── reminder_handler.py    # /reminders management ConversationHandler
│   │   ├── payment_handler.py
│   │   └── admin_handler.py
│   └── keyboards/           # Inline keyboard builders
│
├── components/
│   ├── pregnancy_engine.py       # Gestational age, daily facts, milestones
│   ├── nutrition_assistant.py    # RDI tracking, deficiency alerts
│   ├── symptom_assistant.py      # Trend reports, severity alerts
│   ├── appointment_tracker.py    # Appointment CRUD + auto-reminders
│   ├── reminder_system.py        # Reminder CRUD + timezone conversion
│   ├── doctor_visit_assistant.py # Pre-appointment PDF summary
│   └── preference_engine.py      # Food preference conflict resolution
│
├── schemas/                 # Pydantic extraction schemas (one per record type)
│   ├── meal.py, symptom.py, exercise.py, medication.py
│   ├── weight.py, water.py, question.py, preference.py
│   ├── appointment.py       # NEW: natural language appointment extraction
│   └── reminder.py          # NEW: natural language reminder extraction
│
├── memory/
│   ├── personal_memory.py   # Health record CRUD + visibility enforcement
│   └── family_memory.py     # Family-scoped shared record queries
│
├── models/                  # SQLAlchemy ORM models
├── payment/                 # Razorpay + Stripe clients + state machine
├── knowledge/               # RAG ingestion pipeline + retriever
├── jobs/                    # Scheduled job handlers
├── api/routes/              # FastAPI routes (webhook, payment, jobs, health)
└── services/
    └── admin_service.py     # Admin identity, pending registry, in-memory metrics

infra/                       # CDK deployment stack
tests/unit/                  # 466 unit tests
```

---

## 11. Key Design Decisions

**Natural language over commands.** Users should never need to know slash commands exist. Everything they want to do can be expressed naturally. `/appointments` and `/reminders` remain as optional management interfaces.

**Model decides intent, tools handle persistence.** The LLM classifies what the user wants and extracts structured data. The Python code handles all DB operations. No LLM is given direct DB access.

**Gestational context always injected.** Every LLM response (RAG or fallback) receives the user's current gestational age. The model never answers in a vacuum.

**Role-aware responses.** Partner and mom receive different framings. Partners get "how to support her" framing; moms get first-person health guidance.

**Privacy by architecture.** Health data is stripped at the logging layer before any log entry is written. The structlog processor is not optional.

**Approval gate.** New users don't get access until the admin approves them. This keeps early user quality high.

**Graceful degradation.** Bedrock unavailable → falls to mini tier (OpenAI). RAG empty → LLM answers from general knowledge. Redis unavailable → in-process fallback. Every external dependency has a fallback.

---

## 12. What's Not Built Yet

- Multi-user family query (partner seeing mom's shared data in conversation)
- Knowledge base ingestion UI (currently requires manual script run)
- Push to RDS (currently PostgreSQL on EC2 — fine for early scale, move to RDS when user count justifies it)
- Weekly email digest
- Web dashboard
- iOS/Android native app (Telegram is the distribution channel for now)
