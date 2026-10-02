import atexit
import hashlib
import hmac
import logging
import os
import time
from datetime import date, datetime
from threading import Lock
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, jsonify, request

from academic_assistant import (
    REMINDER_LEAD_DAYS_DEFAULT,
    AcademicItem,
    answer_question,
    format_daily_summary,
    format_deadline_reminder,
    repository_from_environment,
)

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

_scheduler = None
_scheduler_lock = Lock()

_TRUE_VALUES = {"1", "true", "yes", "on"}
_MESSAGE_CHUNK_SIZE = 1800

#: Environment variables reported by the status page (never their values).
CONFIG_VARIABLES = (
    "META_VERIFY_TOKEN",
    "META_APP_SECRET",
    "META_PAGE_ACCESS_TOKEN",
    "ACADEMIC_RECIPIENT_PSID",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "GOOGLE_SHEETS_SPREADSHEET_ID",
    "GOOGLE_SHEETS_RANGE",
    "GOOGLE_DOC_ID",
    "GEMINI_API_KEY",
)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in _TRUE_VALUES


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a whole number.") from error


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required configuration: {name}")
    return value


def _academic_today() -> date:
    timezone = ZoneInfo(os.getenv("ACADEMIC_TIMEZONE", "UTC"))
    return datetime.now(timezone).date()


def _reminder_lead_days() -> int:
    return max(
        0, _env_int("ACADEMIC_REMINDER_DAYS", REMINDER_LEAD_DAYS_DEFAULT)
    )


def _split_message(text: str, limit: int = _MESSAGE_CHUNK_SIZE) -> list[str]:
    """Split a long reply into Messenger-sized chunks on line boundaries."""
    parts = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < 1:
            split_at = limit
        parts.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    return parts


def _messenger_payload(recipient_psid: str, text: str) -> dict:
    payload = {
        "recipient": {"id": recipient_psid},
        "messaging_type": os.getenv("META_MESSAGING_TYPE", "RESPONSE"),
        "message": {"text": text},
    }
    message_tag = os.getenv("META_MESSAGE_TAG")
    if message_tag:
        payload["tag"] = message_tag
    return payload


def _post_messenger(payload: dict) -> None:
    page_token = _required_env("META_PAGE_ACCESS_TOKEN")
    api_version = os.getenv("META_GRAPH_API_VERSION", "v23.0")
    url = f"https://graph.facebook.com/{api_version}/me/messages"
    attempts = 3
    for attempt in range(1, attempts + 1):
        response = requests.post(
            url,
            params={"access_token": page_token},
            json=payload,
            timeout=15,
        )
        if response.ok:
            return
        retryable = response.status_code == 429 or response.status_code >= 500
        if attempt >= attempts or not retryable:
            raise RuntimeError(
                f"Messenger Send API returned HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
        delay = 2 ** (attempt - 1)
        logger.warning(
            "Messenger send failed with HTTP %s; retrying in %ss.",
            response.status_code,
            delay,
        )
        time.sleep(delay)


def _send_messenger_message(recipient_psid: str, text: str) -> None:
    for part in _split_message(text):
        _post_messenger(_messenger_payload(recipient_psid, part))


def _verify_signature(raw_body: bytes) -> bool:
    app_secret = os.getenv("META_APP_SECRET")
    if not app_secret:
        # Local development only: accept webhook calls without a signature while
        # META_APP_SECRET is still being configured.
        if _env_flag("META_ALLOW_UNSIGNED_WEBHOOK"):
            logger.warning(
                "META_APP_SECRET is not set and META_ALLOW_UNSIGNED_WEBHOOK is "
                "enabled; accepting an unsigned webhook. Never use this in "
                "production."
            )
            return True
        return False
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def _load_items(today: date) -> list[AcademicItem]:
    return repository_from_environment().fetch_items(today)


def send_daily_summary() -> str:
    """Send today's/tomorrow's summary and return the message text."""
    today = _academic_today()
    summary = format_daily_summary(_load_items(today), today)
    _send_messenger_message(_required_env("ACADEMIC_RECIPIENT_PSID"), summary)
    logger.info("Sent the daily academic summary.")
    return summary


def send_deadline_reminder() -> str:
    """Send upcoming/overdue deadline reminders and return the message text."""
    today = _academic_today()
    lead_days = _reminder_lead_days()
    message = format_deadline_reminder(_load_items(today), today, lead_days)
    _send_messenger_message(_required_env("ACADEMIC_RECIPIENT_PSID"), message)
    logger.info("Sent deadline reminders (lead time: %d day(s)).", lead_days)
    return message


def _config_status() -> dict:
    return {
        name: "set" if os.getenv(name) else "missing" for name in CONFIG_VARIABLES
    }


def _admin_authorized() -> bool:
    expected = os.getenv("ACADEMIC_ADMIN_TOKEN")
    if not expected:
        return False
    supplied = request.headers.get("X-Admin-Token", "") or request.args.get("token", "")
    return hmac.compare_digest(
        str(supplied).encode("utf-8"), expected.encode("utf-8")
    )


def _admin_denied_response():
    if not os.getenv("ACADEMIC_ADMIN_TOKEN"):
        return (
            jsonify(
                error=(
                    "ACADEMIC_ADMIN_TOKEN is not configured, so manual triggers "
                    "are disabled."
                )
            ),
            503,
        )
    if not _admin_authorized():
        return jsonify(error="Invalid or missing admin token."), 401
    return None


def _reply_for(psid: str, text: str) -> str:
    """Build the reply for one incoming Messenger message."""
    command = text.strip().lower().rstrip("!?. ")
    if command in {"my id", "id", "psid", "whoami"}:
        return (
            "🆔 Your Messenger PSID (page-scoped ID) is:\n"
            f"{psid}\n\n"
            "Put this value in ACADEMIC_RECIPIENT_PSID in your .env file so I can "
            "send you the daily summary and deadline reminders."
        )
    today = _academic_today()
    return answer_question(
        text, _load_items(today), today, _reminder_lead_days(), user_id=psid
    )


def _shutdown_scheduler(scheduler: BackgroundScheduler) -> None:
    """Stop a scheduler on interpreter exit without raising when already stopped."""
    try:
        scheduler.shutdown(wait=False)
    except Exception:  # noqa: BLE001 - shutdown must never raise at exit
        logger.debug("Scheduler was already stopped during shutdown.")


def _parse_hhmm(value: str, name: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.split(":", maxsplit=1)
        hour, minute = int(hour_text), int(minute_text)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must use 24-hour HH:MM format.") from error
    return hour, minute


def _start_scheduler() -> BackgroundScheduler:
    global _scheduler
    with _scheduler_lock:
        if _scheduler and _scheduler.running:
            return _scheduler
        timezone = os.getenv("ACADEMIC_TIMEZONE", "UTC")
        scheduler = BackgroundScheduler(timezone=timezone)

        hour, minute = _parse_hhmm(
            os.getenv("ACADEMIC_NOTIFICATION_TIME", "08:00"),
            "ACADEMIC_NOTIFICATION_TIME",
        )
        scheduler.add_job(
            send_daily_summary,
            trigger="cron",
            hour=hour,
            minute=minute,
            id="daily-academic-summary",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "Daily academic summary scheduled for %02d:%02d (%s).",
            hour,
            minute,
            timezone,
        )

        if _env_flag("ENABLE_REMINDERS", True) and _reminder_lead_days() > 0:
            reminder_hour, reminder_minute = _parse_hhmm(
                os.getenv("ACADEMIC_REMINDER_TIME", "18:00"),
                "ACADEMIC_REMINDER_TIME",
            )
            scheduler.add_job(
                send_deadline_reminder,
                trigger="cron",
                hour=reminder_hour,
                minute=reminder_minute,
                id="deadline-reminder",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info(
                "Deadline reminders scheduled for %02d:%02d (%s), %d day(s) ahead.",
                reminder_hour,
                reminder_minute,
                timezone,
                _reminder_lead_days(),
            )
        else:
            logger.info("Deadline reminder job is disabled.")

        scheduler.start()
        _scheduler = scheduler
        atexit.register(_shutdown_scheduler, scheduler)
        return scheduler


def create_app(start_scheduler: bool = True) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        return jsonify(
            service="Flask Academic Assistant for Messenger",
            status="ok",
            google_config=_config_status(),
            scheduler_jobs=[job.id for job in _scheduler.get_jobs()] if _scheduler else [],
            reminder_lead_days=_reminder_lead_days(),
            endpoints={
                "verify_webhook": "GET /webhook",
                "receive_messages": "POST /webhook",
                "health": "GET /health",
                "preview": "GET /preview",
                "send_summary": "POST /send-summary",
                "send_reminders": "POST /send-reminders",
            },
        )

    @app.get("/health")
    def health():
        return jsonify(status="ok"), 200

    @app.get("/preview")
    def preview():
        try:
            today = _academic_today()
            items = _load_items(today)
        except Exception as error:
            logger.exception("Preview failed to load Google data.")
            return jsonify(error=str(error)), 503
        return jsonify(
            date=today.isoformat(),
            reminder_lead_days=_reminder_lead_days(),
            item_count=len(items),
            summary=format_daily_summary(items, today),
            reminders=format_deadline_reminder(items, today, _reminder_lead_days()),
            items=[
                {
                    "title": item.title,
                    "category": item.category,
                    "details": item.details,
                    "due_date": item.due_date.isoformat() if item.due_date else None,
                    "source": item.source,
                }
                for item in items
            ],
        )

    @app.post("/send-summary")
    def trigger_summary():
        denied = _admin_denied_response()
        if denied:
            return denied
        try:
            message = send_daily_summary()
        except Exception as error:
            logger.exception("Manual summary trigger failed.")
            return jsonify(error=str(error)), 502
        return jsonify(status="sent", kind="daily-summary", message=message), 200

    @app.post("/send-reminders")
    def trigger_reminders():
        denied = _admin_denied_response()
        if denied:
            return denied
        if _reminder_lead_days() <= 0:
            return jsonify(error="ACADEMIC_REMINDER_DAYS must be greater than 0."), 400
        try:
            message = send_deadline_reminder()
        except Exception as error:
            logger.exception("Manual reminder trigger failed.")
            return jsonify(error=str(error)), 502
        return jsonify(status="sent", kind="deadline-reminder", message=message), 200

    @app.get("/webhook")
    def verify_webhook():
        expected_token = os.getenv("META_VERIFY_TOKEN")
        if (
            request.args.get("hub.mode") == "subscribe"
            and expected_token
            and request.args.get("hub.verify_token") == expected_token
        ):
            return request.args.get("hub.challenge", ""), 200
        return "Verification failed", 403

    @app.post("/webhook")
    def receive_webhook():
        raw_body = request.get_data(cache=True)
        if not _verify_signature(raw_body):
            logger.warning("Rejected Messenger webhook with missing or invalid signature.")
            return jsonify(error="Invalid webhook signature."), 401
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="Webhook payload must be a JSON object."), 400
        if payload.get("object") != "page":
            return jsonify(error="Unsupported webhook object."), 404

        replies_sent = 0
        for entry in payload.get("entry", []):
            for event in entry.get("messaging", []):
                message = event.get("message", {})
                sender_psid = event.get("sender", {}).get("id")
                text = message.get("text", "").strip()
                if not sender_psid or not text or message.get("is_echo"):
                    continue
                logger.info("Received incoming message from %s: %r", sender_psid, text)
                try:
                    reply = _reply_for(sender_psid, text)
                    _send_messenger_message(sender_psid, reply)
                    logger.info("Sent reply to %s", sender_psid)
                    replies_sent += 1
                except Exception:
                    # Answer 200 anyway so Messenger does not retry the whole batch.
                    logger.exception(
                        "Failed to answer the Messenger message from %s.", sender_psid
                    )
        return jsonify(status="ok", replies_sent=replies_sent), 200

    if start_scheduler and _env_flag("ENABLE_SCHEDULER", True):
        _start_scheduler()
    return app


app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "5000")),
        debug=False,
        use_reloader=False,
    )
