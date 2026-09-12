# 🏗️ Architecture — الكوتش الذكي

This document explains *how* the system is put together. Read [`README.md`](../README.md) first
for features and setup.

## 1. Layers

```
Telegram update
      │
      ▼
┌──────────────────────────────────────────────────────────────┐
│ middlewares/auth.py   (outer → inner)                        │
│  session: opens an async DB session per update, commits/closes│
│  services: builds the Services container (repos + AI + media)│
│  throttle: max_updates_per_minute / max_updates_per_hour     │
│  gates: banned? forced channel member? privacy consent?      │
└──────────────────────────────────────────────────────────────┘
      │
      ▼
┌──────────────────────────────────────────────────────────────┐
│ handlers/  (12 routers, 152 handlers)                        │
│  start · onboarding · consult · coach · scan · progress ·    │
│  plans · points · settings · media · admin · fallback        │
└──────────────────────────────────────────────────────────────┘
      │            (handlers are thin: parse → call → render)
      ▼
┌──────────────────────────────────────────────────────────────┐
│ services/  (business rules, unit-testable without Telegram)   │
└──────────────────────────────────────────────────────────────┘
      │                                    │
      ▼                                    ▼
┌───────────────────────────┐   ┌──────────────────────────────┐
│ db/repositories/ (6)      │   │ ai/ (NanoGPT client + router) │
│ users tracking economy    │   │ prompts · schemas · youtube   │
│ plans ops                 │   └──────────────────────────────┘
└───────────────────────────┘                 │
      │                                       ▼
      ▼                              https://nano-gpt.com/api/v1
  PostgreSQL / SQLite
  + storage backend (Local FS or S3/R2)
```

**Design rules**

* Handlers never write SQL and never call the AI provider directly — always through `services.*`.
* Services never import aiogram types except `Bot` (only `broadcast.py` and `media.py` need it),
  which is why the whole suite is testable without Telegram.
* Repositories are the only place that touches the ORM; they never commit (the middleware does),
  except `broadcast.run()` / `reminders.run_once()` which open their own sessions for long jobs.
* `app/callbacks.py` is a **leaf module** (no app imports) so `keyboards.py` can use it without cycles.
* Every timestamp stored is UTC-aware. `app.db.models.as_aware()` normalises naive values coming
  back from SQLite (Postgres returns aware ones) — **always** wrap a DB datetime before arithmetic.

## 2. Data model (21 tables)

| Group | Tables |
|---|---|
| Identity | `users`, `user_badges`, `referral_events`, `privacy_consent` |
| Tracking | `daily_logs`, `weight_records`, `body_measurements`, `meal_entries`, `progress_photos`, `plan_adjustments` |
| Economy | `points_ledger`, `subscriptions`, `admin_actions` |
| Content | `workout_plans`, `chat_messages`, `reminders`, `exercise_videos`, `food_items` |
| Ops | `broadcasts`, `broadcast_targets`, `media_files`, `app_settings`, `ai_usage` |

Key relationships: `users` is the hub; `daily_logs` is one row per user per day holding
consumption + adherence; `meal_entries`/`progress_photos` hang off it; `points_ledger` is
append-only (balances live on `users` and are moved inside the same transaction).

## 3. AI pipeline

```
service call (coach/vision/plans/consult)
   │
   ├─ router.models_for(task)      ← app_settings override (admin /route) or config default
   ├─ router.ensure_budget(...)    ← global daily USD cap + per-user points caps
   │
   ├─ for model in candidates:      # fallback chain, stop on AIRateLimitError
   │     client.complete_once(model, messages, json_schema=?, vision_image=?)
   │        → retry on timeout/HTTP 429/5xx with exponential backoff
   │        → parse JSON (strict schema) or text
   │        → log ai_usage (tokens, cost, latency, task, model, success)
   │
   └─ raise AIModelUnavailableError when every candidate failed
```

* **Cost**: taken from the provider response when present, otherwise `tokens × config price`.
* **Points**: `points_from_tokens()` clamps a message between `COACH_MIN/MAX_POINTS_PER_MESSAGE`.
* **Vision images**: JPEG, optimised and capped at `MAX_IMAGE_BYTES`/`MAX_IMAGE_DIMENSION` before upload.
* **Web search**: `includeDomains=["youtube.com"]` gives real exercise links with no YouTube key;
  results are cached in `exercise_videos` for `VIDEO_CACHE_DAYS`.

## 4. The three products

**A — Free consultation** (`services/consult.py`)
`free_consults_left` decremented per message → `AI_TASK_CONSULTATION` with a persona prompt that
forbids generic advice → answer + video links from the web search cache. At zero the user is
upsold to the coach.

**B — 24h personal coach** (`services/coach.py`)
`unlock("coach")` charges `COACH_PRICE_POINTS` and stores a rolling memory profile.
`handle_message()` first parses locally (weight / water / meal skipped / workout done) and writes
tracking rows **before** the AI call, so numbers are never lost to a model failure. The AI reply
carries `.events` for the UI and `.charge` for the ledger.

**C — Food photo scan** (`services/vision.py`)
`analyse_food()` → structured JSON. If `needs_clarification` is set the service returns
`clarifications` (up to 2) and **never** writes a meal; otherwise `log_meal()` stores
`MealEntry` + `ProgressPhoto`-style media and deducts from today's budget.

## 5. Adaptive logic

| Trigger | Where | Effect |
|---|---|---|
| 2-week weight plateau | `metabolism.recalibrate()` | ±calories/macro adjustment, logged to `plan_adjustments` |
| Skipped meal | `day_recalculation` schema | remaining meals re-planned within the same calorie target |
| Injury / red-flag condition | `services/safety.py` | exercises excluded, safe alternatives suggested, medical referral text |
| Low adherence | `services/streak.py` | streak break + re-engagement reminder |
| Budget | `plans.generate(user, "nutrition")` | local/cheap foods (`budget_mode`), disliked foods and allergies respected |
| Cost cap hit | `router.ensure_budget()` | `AIBudgetExceeded` → friendly "budget hit" message, no charge |

## 6. Jobs (`app/jobs/scheduler.py`)

| Job | Interval | What it does |
|---|---|---|
| `reminders.run_once` | `REMINDER_INTERVAL_S` | due rules → send → re-arm `next_run_at` |
| `broadcaster.run_pending` | 60 s | scheduled broadcasts |
| `subscription.run_auto_renewals` | hourly | renew expiring subscriptions, grant points |
| `privacy.purge_expired_media` | daily | delete media older than retention |
| `media.refresh_stale_video_cache` | daily | refresh exercise-video cache |
| `reports.daily_digest` | `DIGEST_SEND_HOUR` (user TZ) | daily summary to coach subscribers |
| `usage.prune_old_logs` | weekly | trim `ai_usage` history |

All jobs are best-effort: exceptions are logged, never kill the scheduler.

## 7. Failure modes & graceful degradation

| Failure | Behaviour |
|---|---|
| No `BOT_TOKEN` | `main.py` logs a clear Arabic+English message and exits non-zero |
| No `NANOGPT_API_KEY` | AI calls raise `AIConfigurationError`; handlers render a polite "service unavailable" text and **do not charge** |
| Provider timeout / 429 / 5xx | retry with backoff, then next model in the chain |
| All models down | `AIModelUnavailableError` → user sees "the coach is busy, try again" + admin alert log |
| Bad JSON from model | schema re-ask once, then fallback parser, then treat as failure (no partial charges) |
| DB unavailable at startup | startup aborts with the migration hint in the log |
| User blocked the bot | `TelegramForbiddenError` caught → rule/subscription marked inactive, no retries |
| Message > 4096 chars | `utils/tg.py` splits on paragraph/word boundaries |

## 8. Configuration

`app/config.py` exposes ~124 settings from `.env` (see `.env.example`, which documents every one).
Categories: Telegram, database, storage, NanoGPT + models + pricing, points economy, caps,
reminders, safety, i18n, privacy, admin, webhook. Anything the product team may want to tune
(price of a scan, daily caps, quiet hours, retention) is a setting — no code edit required.

## 9. Testing strategy

* **No network**: `tests/conftest.py` monkeypatches `NanoGPTClient.complete/transcribe/web_search`
  with schema-valid canned payloads (override per test via `tests.conftest.CANNED`).
* **SQLite** in-memory/temp file with `DB_AUTO_CREATE=true`; Postgres is exercised through the
  same repositories (types are portable: `JSONType`, `Uuid`, TZ `DateTime`).
* **FakeBot** implements only what the services use (`send_message`, `send_photo`, `get_me`,
  `answer_callback_query`), so broadcast/reminder tests run for real against it.
* **Handler layer**: `tests/telegram_mock.py` provides a `MockedSession(BaseSession)` that records
  every Bot API call and answers with valid objects (including bot-mounted messages, so
  `status.edit_text()` chains work). `tests/test_handlers.py` feeds genuine `Update` objects
  through the *production* dispatcher — middlewares, gates, FSM and all 152 handlers included.
* **Static guards** (`test_i18n_keyboards.py`) walk the AST of `app/` for two bug classes that
  only explode at send time: a bare `InlineKeyboardBuilder` passed as `reply_markup`, and long
  literals packed into callback data (Telegram's cap is 64 bytes; Arabic is ~2 bytes/char).
* 315 tests across 11 files — see the coverage table in `README.md`.

### Bugs this layer caught (all fixed)

| Bug | Impact |
|---|---|
| `CommandStart(deep_link=True)` only matches `/start <payload>` | `/start` never reached its handler — new users got the "unknown command" list |
| Gate exemptions read `data["handler"]` from an *outer* middleware | aiogram sets that key only inside `trigger()`, so the consent gate blocked `/start` **and** the "accept" button → new users could never get in |
| 25 handlers declared the callback param as `cb` | aiogram injects it as `callback_data` → every one of those buttons raised `TypeError` |
| `_injury_keyboard()` returned a builder, not a markup | the injury step crashed → onboarding could not be completed |
| Consultation examples packed whole Arabic sentences into callback data | >64 bytes → the entire consult panel failed to render |
| `ChatActionSender.uploading_photo` (correct name: `upload_photo`) | every photo-upload path crashed |
| `admin.py` called `load_overrides/describe/set_override` on the router *module* | the model-routing panel and `/route` crashed |
| `rule.time_of_day` (model has `hour`/`minute`) | the reminder toggle button crashed |
| Telegram's client locale overwrote the chosen language on every update | switching to English reverted to Arabic on the next message |
| `editMessageText` sent with a reply keyboard | "re-check channel subscription" crashed instead of showing the menu |
| `BroadcastService.create()` opened a second session | on SQLite (documented dev path) → "database is locked"; now reuses the request session |
| GDPR wipe left body metrics/daily logs behind | `deactivate_user_data` now clears them and deletes `daily_logs` |
| `CommandStart(deep_link=True)` swallowed bare `/start` | see the table above — fixed by registering both filters |
| `ALLOWED_UPDATES` asked Telegram for 4 update types nothing handled | trimmed to `message`/`callback_query`/`my_chat_member`, and a `my_chat_member` handler now pauses reminders when a user blocks the bot and re-arms only those it paused when they return |
| Photo deletion only removed progress photos | food photos are personal images too — now purged as well |
