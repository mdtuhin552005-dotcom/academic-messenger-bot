# Academic Assistant for Messenger (Flask + Google Sheets/Docs)

A Flask webhook that turns a Google Sheet and a Google Doc into a Messenger
study assistant. It answers questions automatically, sends a daily summary and
reminds you about upcoming and overdue deadlines.

Requires Python 3.9 or newer (developed and tested with the bundled `.venv`).

## What it does

- **Auto reply in Messenger** — every message you send to the Page is answered
  from the current Sheet/Doc content (`today`, `tomorrow`, `this week`,
  `reminders`, or any keyword such as a course or assignment name).
- **Daily summary** — every day at `ACADEMIC_NOTIFICATION_TIME` it sends the
  tasks due today and tomorrow.
- **Deadline reminders** — every day at `ACADEMIC_REMINDER_TIME` it sends
  overdue plus upcoming deadlines for the next `ACADEMIC_REMINDER_DAYS` days.
- **Manual triggers** — `POST /send-summary` and `POST /send-reminders`
  (protected by `ACADEMIC_ADMIN_TOKEN`).
- **Preview without Messenger** — `GET /preview` shows exactly what would be
  sent, so you can test Google access before touching Meta.
- **PSID onboarding** — send `my id` to the Page and it replies with your
  Messenger PSID, which you put in `ACADEMIC_RECIPIENT_PSID`.
- **Robust Google access** — retries transient API failures, and keeps working
  from the Sheet if the Doc fails (or the other way round).

## How it works

```
Messenger user ──message──▶ Meta webhook ──POST /webhook──▶ Flask app
                                                              │
                                     Google Sheets API ◀──────┤
                                     Google Docs API  ◀───────┤
                                                              │
              Meta Send API ◀──auto reply/summary/reminder────┘
```

`academic_assistant.py` holds the data model, Sheet/Doc parsing, question
answering and message formatting. `app.py` holds the Flask routes, the
Messenger client and the APScheduler jobs. All logic is unit-tested in `tests/`.

```
project 1/
├─ app.py                    # Flask routes, Messenger client, scheduler
├─ academic_assistant.py     # Sheet/Doc parsing, answers, summaries, reminders
├─ tests/
│  ├─ test_academic_assistant.py
│  └─ test_app.py
├─ requirements.txt
├─ .env.example              # copy to .env and fill in
└─ README.md
```

## 1. Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env      # then edit .env
python app.py                    # serves http://localhost:5000
```

Check that the process is healthy and see which settings are still missing:

```powershell
Invoke-RestMethod http://localhost:5000/           # config + scheduler jobs
Invoke-RestMethod http://localhost:5000/health     # {"status":"ok"}
Invoke-RestMethod http://localhost:5000/preview    # summary + reminders preview
```

Once Google is configured, `/preview` returns the Sheet/Doc items, the daily
summary text and the reminder text — without sending anything to Messenger.

## 2. Google Cloud setup (Sheet + Doc)

1. In <https://console.cloud.google.com> create (or pick) a project.
2. **APIs & Services → Library**: enable **Google Sheets API** and
   **Google Docs API**.
3. **APIs & Services → Credentials → Create credentials → Service account**.
   Give it any name, then open it → **Keys → Add key → Create new key → JSON**.
   Save the file outside the project (for example `C:\secure\service-account.json`).
4. Copy the service account email (`something@project.iam.gserviceaccount.com`).
5. Open your spreadsheet and document and click **Share**, adding that
   service-account email with **Viewer** access. Without this step the API
   returns `403 PERMISSION_DENIED`.
6. Set in `.env`:
   - `GOOGLE_APPLICATION_CREDENTIALS` — full path to the JSON key file.
   - `GOOGLE_SHEETS_SPREADSHEET_ID` — from the Sheet URL
     `https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit`.
   - `GOOGLE_SHEETS_RANGE` — for example `"'Academic Schedule'!A:D"`.
   - `GOOGLE_DOC_ID` — from the Doc URL
     `https://docs.google.com/document/d/<DOC_ID>/edit` (optional).

### Sheet format

The first row must be a header row. A date column and a task column are
required; the type and details columns are optional.

| Due Date   | Type       | Task         | Details        |
| ---------- | ---------- | ------------ | -------------- |
| 2026-10-03 | Assignment | Essay        | Read chapter 3 |
| 2026-10-05 | Quiz       | Physics quiz | Chapters 4-5   |

Accepted header names (case-insensitive):

- date column: `Date`, `Due Date`, `Deadline`, `Due`, `Class Date`
- task column: `Task`, `Assignment`, `Title`, `Name`, `Event`
- optional type column: `Type`, `Category`, `Course`
- optional details column: `Details`, `Description`, `Notes`

Accepted date formats: `2026-10-03`, `10/03/2026`, `10/03/26`, `10/03`,
`10-03-2026`, `October 3, 2026`, `Oct 3`. Dates without a year use the current
year. A row without a task is skipped; an unparsable date raises an error that
names the offending row.

### Doc format

Write notes as normal paragraphs. A paragraph that contains one or more dates
becomes one item per date; paragraphs without a date are still searchable when
answering questions. Table cells and the table of contents are read too.

```
2026-10-03 — Project proposal due
Proposal due 2026-10-03; presentation 2026-10-04
Course policy: attendance matters
```


## 3. Meta / Messenger setup

1. Create an app at <https://developers.facebook.com/apps> with the
   **Messenger** product (type "Business").
2. **Messenger → Settings → Access Tokens**: connect your Facebook Page and
   generate a **Page access token**. Put it in `META_PAGE_ACCESS_TOKEN`.
3. **Settings → Basic**: copy the **App Secret** into `META_APP_SECRET`. It is
   used to verify `X-Hub-Signature-256` on every webhook POST, so keep it secret.
4. Choose any random string for `META_VERIFY_TOKEN` — you will paste the same
   value into the Meta webhook settings.
5. Expose your local app (next section) and set the **Callback URL** to
   `https://<your-public-host>/webhook` with the same verify token. Click
   **Verify and save**; the app answers `GET /webhook` with `hub.challenge`.
6. Subscribe the webhook to the **messages** field for your Page.
7. Send any message to your Page from your own Facebook account. The assistant
   replies with a short help text. Then send `my id`, copy the PSID it returns
   into `ACADEMIC_RECIPIENT_PSID`, and restart the app — that is the recipient
   for the daily summary and deadline reminders.

### Local development with ngrok

```powershell
ngrok http 5000      # copy the https URL, e.g. https://abcd-1-2-3-4.ngrok-free.app
```

Use `https://<ngrok-host>/webhook` as the Callback URL in the Meta app.

To test the webhook locally *before* `META_APP_SECRET` is known, set
`META_ALLOW_UNSIGNED_WEBHOOK=true` (development only — strict signature
checking returns as soon as the secret is present, and never use this in
production):

```powershell
$body = '{"object":"page","entry":[{"messaging":[{"sender":{"id":"123"},"message":{"text":"help"}}]}]}'
Invoke-RestMethod http://localhost:5000/webhook -Method Post -Body $body -ContentType 'application/json'
```

### Messenger delivery rules

The recipient must have messaged the Page first (outside the 24-hour response
window Meta only allows approved message tags). For reminders that may arrive
outside that window, set `META_MESSAGE_TAG=CONFIRMED_EVENT_UPDATE` and keep
`META_MESSAGING_TYPE=RESPONSE`; leave `META_MESSAGE_TAG` empty for plain
replies to incoming messages.

## 4. Endpoints

| Method | Path               | Purpose                                                   |
| ------ | ------------------ | --------------------------------------------------------- |
| GET    | `/`                | Status page: config set/missing, scheduler jobs.           |
| GET    | `/health`          | Liveness probe → `{"status":"ok"}`.                        |
| GET    | `/preview`         | Items, daily summary and reminders text, nothing is sent.  |
| GET    | `/webhook`         | Meta webhook verification (`hub.challenge`).               |
| POST   | `/webhook`         | Incoming Messenger messages (signature verified).          |
| POST   | `/send-summary`    | Send the daily summary now (`ACADEMIC_ADMIN_TOKEN`).       |
| POST   | `/send-reminders`  | Send deadline reminders now (`ACADEMIC_ADMIN_TOKEN`).      |

Manual triggers accept the token either as `X-Admin-Token: <token>` or as a
`?token=<token>` query parameter. If `ACADEMIC_ADMIN_TOKEN` is empty the
endpoints answer `503` (disabled), which is the safe default:

```powershell
Invoke-RestMethod http://localhost:5000/send-reminders -Method Post -Headers @{ 'X-Admin-Token' = 'my-secret' }
```

## 5. Configuration reference

| Variable                         | Default           | Purpose                                     |
| -------------------------------- | ----------------- | ------------------------------------------- |
| `META_VERIFY_TOKEN`              | –                 | Webhook verification token (matches Meta).  |
| `META_APP_SECRET`                | –                 | Verifies `X-Hub-Signature-256`.             |
| `META_PAGE_ACCESS_TOKEN`         | –                 | Page token used to send messages.           |
| `ACADEMIC_RECIPIENT_PSID`        | –                 | Who receives summary + reminders.           |
| `META_GRAPH_API_VERSION`         | `v23.0`           | Graph API version for `/me/messages`.       |
| `META_MESSAGING_TYPE`            | `RESPONSE`        | `messaging_type` sent to the Send API.      |
| `META_MESSAGE_TAG`               | –                 | Approved tag for out-of-window reminders.   |
| `META_ALLOW_UNSIGNED_WEBHOOK`    | `false`           | Development only: skip signature checking.  |
| `GOOGLE_APPLICATION_CREDENTIALS` | –                 | Path to the service-account JSON key.       |
| `GOOGLE_SHEETS_SPREADSHEET_ID`   | –                 | Spreadsheet ID.                             |
| `GOOGLE_SHEETS_RANGE`            | –                 | Range, e.g. `"'Academic Schedule'!A:D"`.    |
| `GOOGLE_DOC_ID`                  | –                 | Doc ID (optional; empty = Sheet only).      |
| `ACADEMIC_NOTIFICATION_TIME`     | `08:00`           | Daily summary time (HH:MM).                 |
| `ACADEMIC_REMINDER_TIME`         | `18:00`           | Deadline reminder time (HH:MM).             |
| `ACADEMIC_REMINDER_DAYS`         | `3`               | Reminder look-ahead; `0` disables the job.  |
| `ACADEMIC_TIMEZONE`              | `UTC`             | IANA timezone, e.g. `Asia/Dhaka`.           |
| `ENABLE_SCHEDULER`               | `true`            | Start the background jobs with the app.     |
| `ENABLE_REMINDERS`               | `true`            | Enable/disable the reminder job only.       |
| `ACADEMIC_ADMIN_TOKEN`           | –                 | Enables the manual trigger endpoints.       |
| `LOG_LEVEL`                      | `INFO`            | Python logging level.                       |
| `HOST` / `PORT`                  | `0.0.0.0` / `5000`| Bind address for `python app.py`.           |

## 6. Tests

```powershell
python -m unittest discover -s tests -v
```

The suite is fully offline: Messenger sending and Google access are mocked, and
the scheduler is disabled. It covers Sheet/Doc parsing, date handling, question
answers, help/reminder commands, webhook verification and signature rejection,
PSID replies, preview output, manual triggers and scheduler job registration.

## 7. Deployment notes

- Run **one** scheduler process. The in-process APScheduler is not coordinated
  across WSGI workers: either run a single process, or run webhook workers with
  `ENABLE_SCHEDULER=false` and one dedicated scheduler process.
- Serve through HTTPS (Meta requires it) behind a supervisor that restarts the
  app on failure, for example:

  ```powershell
  pip install waitress
  waitress-serve --listen=0.0.0.0:5000 app:app
  ```

- Google and Messenger calls are retried on transient failures; a failure while
  answering one message is logged without failing the whole webhook batch.
- Never commit `.env` or the service-account JSON — both are already listed in
  `.gitignore`.

## 8. Troubleshooting

| Symptom | Fix |
| ------- | --- |
| `Missing required Google configuration` | Fill the Google variables in `.env`. |
| `403 PERMISSION_DENIED` from Google | Share the Sheet/Doc with the service-account email as Viewer. |
| `Unable to load academic data` in `/preview` | Check the key path, range name and Doc ID. |
| `Google Sheet needs a Date/Due Date column...` | Rename the header row using an accepted name. |
| Webhook verification fails | `META_VERIFY_TOKEN` must match exactly; URL must be public HTTPS. |
| Webhook returns `401 Invalid webhook signature` | Wrong `META_APP_SECRET`, or testing before it is set. |
| No summary/reminder arrives | Check `/` for scheduler jobs, confirm the PSID, and remember Meta's 24-hour window rules. |
| `ACADEMIC_NOTIFICATION_TIME must use 24-hour HH:MM` | Use `08:00` / `18:30`, not `8 AM`. |

## 9. বাংলা দ্রুত সেটআপ (Bengali quick start)

1. **Google Sheet** বানান — প্রথম সারির হেডিং: `Due Date`, `Type`, `Task`,
   `Details`। যেমন `2026-10-03 | Assignment | Essay | Read chapter 3`।
2. **Google Doc** (ঐচ্ছিক) — প্যারাগ্রাফে তারিখ লিখুন: `2026-10-03 — Project
   proposal due`। তারিখ ছাড়া লেখাও প্রশ্নের উত্তরে খুঁজে পাওয়া যাবে।
3. **Google Cloud** → Sheets API ও Docs API চালু করুন → service account বানিয়ে
   JSON key ডাউনলোড করুন → সেই service account email দিয়ে Sheet ও Doc
   **Share (Viewer)** করুন। `.env`-এ `GOOGLE_APPLICATION_CREDENTIALS`,
   `GOOGLE_SHEETS_SPREADSHEET_ID`, `GOOGLE_SHEETS_RANGE`, `GOOGLE_DOC_ID` দিন।
4. **Meta app** → Messenger product যোগ করে Page access token এবং App Secret
   নিন। `.env`-এ `META_PAGE_ACCESS_TOKEN`, `META_APP_SECRET`,
   `META_VERIFY_TOKEN` দিন।
5. `python app.py` চালিয়ে `ngrok http 5000` দিয়ে public URL নিন, সেটাই Meta-র
   webhook Callback URL (`https://.../webhook`) হিসেবে দিন এবং `messages`
   field subscribe করুন।
6. আপনার Facebook account থেকে Page-এ **`help`** পাঠান — সঙ্গে সঙ্গে auto reply
   পাবেন। এরপর **`my id`** পাঠিয়ে PSID নিয়ে `.env`-এর
   `ACADEMIC_RECIPIENT_PSID`-এ বসান। এখন প্রতিদিন নির্দিষ্ট সময়ে summary ও
   deadline reminder চলে আসবে।
7. সময়/অঞ্চল ঠিক করুন: `ACADEMIC_TIMEZONE=Asia/Dhaka`,
   `ACADEMIC_NOTIFICATION_TIME=08:00`, `ACADEMIC_REMINDER_TIME=18:00`,
   `ACADEMIC_REMINDER_DAYS=3`।
8. পরীক্ষা: `python -m unittest discover -s tests -v` এবং
   `Invoke-RestMethod http://localhost:5000/preview`।

Messenger-এ পাঠানো যাবে এমন কমান্ড: `help`, `today`, `tomorrow`, `this week`,
`reminders`, `my id`, অথবা যেকোনো keyword (যেমন subject বা assignment-এর নাম)।

## 10. Privacy and platform notes

Google calls happen on each incoming question, so answers always use the latest
Sheet/Doc content. Nothing is stored in a database; the app keeps no message
history. The implementation uses keyword/date matching rather than an external
generative AI service. A Page cannot message an arbitrary Facebook ID — only
users who opted in by messaging the Page first, within Meta's messaging window
and approved notification use cases. The app verifies `X-Hub-Signature-256`
using `META_APP_SECRET` before handling webhook POST requests.

