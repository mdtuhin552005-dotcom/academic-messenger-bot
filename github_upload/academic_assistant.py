import base64
import concurrent.futures
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Callable, Iterable, Optional

import requests

logger = logging.getLogger(__name__)

REMINDER_LEAD_DAYS_DEFAULT = 3

HELP_TEXT = (
    "👋 I am your academic assistant. I read your Google Sheet and Google Doc "
    "and reply with your tasks, deadlines and reminders.\n\n"
    "Try messages like:\n"
    "• today\n"
    "• tomorrow\n"
    "• this week\n"
    "• reminders\n"
    "• assignment, quiz, course name or any keyword\n\n"
    "I also send a daily summary and a deadline reminder automatically."
)

GREETING_WORDS = {"help", "start", "hi", "hello", "hey", "menu", "commands"}
REMINDER_WORDS = {"reminder", "reminders", "upcoming", "deadline", "deadlines", "week", "this week"}


@dataclass(frozen=True)
class AcademicItem:
    title: str
    details: str = ""
    category: str = "Task"
    due_date: Optional[date] = None
    source: str = "Google Sheet"

    @property
    def searchable_text(self) -> str:
        return f"{self.category} {self.title} {self.details} {self.source}".strip()


_DATE_PATTERNS = (
    re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b"),
    re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b"),
    re.compile(
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\s+\d{1,2}(?:,?\s+\d{4})?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b\d{1,2}(?:st|nd|rd|th)?(?:\s*[-/–—]?\s*|\s+)(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)(?:\s*,?\s*\d{4})?\b",
        re.IGNORECASE,
    ),
)


def parse_date(value: str, current_date: Optional[date] = None) -> Optional[date]:
    value = value.strip()
    if not value:
        return None
    current_date = current_date or date.today()
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", value, flags=re.IGNORECASE)
    cleaned = re.sub(r"[-–—/]+", " ", cleaned).strip()
    formats = (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%m/%d",
        "%m-%d-%Y",
        "%m-%d-%y",
        "%m-%d",
        "%B %d, %Y",
        "%B %d %Y",
        "%B %d",
        "%b %d, %Y",
        "%b %d %Y",
        "%b %d",
        "%d %B %Y",
        "%d %B",
        "%d %b %Y",
        "%d %b",
    )
    for target_val in (cleaned, value):
        for date_format in formats:
            try:
                if "%Y" not in date_format and "%y" not in date_format:
                    parsed = datetime.strptime(f"{target_val} {current_date.year}", f"{date_format} %Y").date()
                else:
                    parsed = datetime.strptime(target_val, date_format).date()
                return parsed
            except ValueError:
                continue
    raise ValueError(f"Unsupported date value: {value!r}")


def _column_index(headers: list[str], names: Iterable[str]) -> Optional[int]:
    normalized = [header.strip().lower() for header in headers]
    for name in names:
        if name in normalized:
            return normalized.index(name)
    return None


def items_from_sheet_rows(
    rows: list[list[str]], current_date: Optional[date] = None
) -> list[AcademicItem]:
    if not rows:
        return []
    headers = [str(value) for value in rows[0]]
    date_column = _column_index(
        headers, ("date", "due date", "deadline", "due", "class date")
    )
    title_column = _column_index(
        headers, ("task", "assignment", "title", "name", "event")
    )
    category_column = _column_index(headers, ("type", "category", "course"))
    details_column = _column_index(headers, ("details", "description", "notes"))
    if date_column is None or title_column is None:
        raise ValueError(
            "Google Sheet needs a Date/Due Date column and a Task/Assignment column."
        )

    result = []
    for row_number, row in enumerate(rows[1:], start=2):
        def cell(column: Optional[int]) -> str:
            if column is None or column >= len(row):
                return ""
            return str(row[column]).strip()

        title = cell(title_column)
        if not title:
            continue
        raw_date = cell(date_column)
        try:
            item_date = parse_date(raw_date, current_date) if raw_date else None
        except ValueError as error:
            raise ValueError(f"Invalid date in Google Sheet row {row_number}: {error}") from error
        result.append(
            AcademicItem(
                title=title,
                details=cell(details_column),
                category=cell(category_column) or "Task",
                due_date=item_date,
            )
        )
    return result


def _google_doc_paragraphs(content: list[dict]) -> list[str]:
    paragraphs = []
    for element in content:
        paragraph = element.get("paragraph")
        if paragraph:
            text = "".join(
                run.get("textRun", {}).get("content", "")
                for run in paragraph.get("elements", [])
            ).strip()
            if text:
                paragraphs.append(text)
        table = element.get("table")
        if table:
            for row in table.get("tableRows", []):
                for cell in row.get("tableCells", []):
                    paragraphs.extend(_google_doc_paragraphs(cell.get("content", [])))
        table_of_contents = element.get("tableOfContents")
        if table_of_contents:
            paragraphs.extend(
                _google_doc_paragraphs(table_of_contents.get("content", []))
            )
    return paragraphs


def _dates_in_text(text: str, current_date: date) -> list[date]:
    found = []
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            try:
                parsed = parse_date(match.group(0), current_date)
            except ValueError:
                continue
            if parsed and parsed not in found:
                found.append(parsed)
    return found


WEEKDAYS_MAP = {
    "monday": 0, "sombar": 0, "সোমবার": 0,
    "tuesday": 1, "mongolbar": 1, "মঙ্গলবার": 1,
    "wednesday": 2, "budhbar": 2, "বুধবার": 2,
    "thursday": 3, "brihospotibar": 3, "বৃহস্পতিবার": 3,
    "friday": 4, "shukrobar": 4, "শুক্রবার": 4,
    "saturday": 5, "shonibar": 5, "শনিবার": 5,
    "sunday": 6, "robibar": 6, "রবিবার": 6,
}


def _parse_weekday(text: str, current_date: date) -> Optional[date]:
    clean = text.strip().lower().rstrip(":.-")
    for name, day_idx in WEEKDAYS_MAP.items():
        if clean == name or clean.startswith(name + " ") or clean.startswith(name + ":"):
            diff = (day_idx - current_date.weekday()) % 7
            return current_date + timedelta(days=diff)
    return None


def items_from_doc(document: dict, current_date: Optional[date] = None) -> list[AcademicItem]:
    current_date = current_date or date.today()
    result = []
    tabs = document.get("tabs", [])

    def process_body(paragraphs: list[str], source_name: str) -> None:
        last_heading_date = None
        for paragraph in paragraphs:
            dates = _dates_in_text(paragraph, current_date)
            weekday_date = _parse_weekday(paragraph, current_date) if not dates else None
            if dates:
                last_heading_date = dates[0]
                for item_date in dates:
                    result.append(
                        AcademicItem(
                            title=paragraph,
                            category="Project / document",
                            due_date=item_date,
                            source=source_name,
                        )
                    )
            elif weekday_date:
                last_heading_date = weekday_date
                result.append(
                    AcademicItem(
                        title=paragraph,
                        category="Routine",
                        due_date=weekday_date,
                        source=source_name,
                    )
                )
            else:
                result.append(
                    AcademicItem(
                        title=paragraph,
                        category="Project / document",
                        due_date=last_heading_date,
                        source=source_name,
                    )
                )

    if tabs:
        for tab in tabs:
            tab_title = tab.get("tabProperties", {}).get("title", "Google Doc")
            doc_tab = tab.get("documentTab", {})
            body = doc_tab.get("body", {}).get("content", [])
            process_body(_google_doc_paragraphs(body), f"Google Doc ({tab_title})")
    else:
        body = document.get("body", {}).get("content", [])
        process_body(_google_doc_paragraphs(body), "Google Doc")

    return result


def execute_with_retry(
    call: Callable[[], dict],
    attempts: int = 3,
    base_delay: float = 1.0,
    description: str = "Google API call",
) -> dict:
    """Run ``call`` retrying transient network/API failures with backoff."""
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as error:  # noqa: BLE001 - Google raises several error types
            last_error = error
            if attempt >= attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "%s failed (attempt %d/%d): %s. Retrying in %.0fs.",
                description,
                attempt,
                attempts,
                error,
                delay,
            )
            time.sleep(delay)
    raise RuntimeError(f"{description} failed after {attempts} attempts: {last_error}")


class GoogleAcademicRepository:
    """Reads academic items from a Google Sheet and (optionally) a Google Doc."""

    def __init__(
        self,
        spreadsheet_id: str,
        sheet_range: str,
        credentials_path: Optional[str] = None,
        document_id: Optional[str] = None,
        credentials_json: Optional[str] = None,
    ):
        self.spreadsheet_id = spreadsheet_id
        self.sheet_range = sheet_range
        self.document_id = document_id
        self.credentials_path = credentials_path
        self.credentials_json = credentials_json

    def _credentials(self):
        from google.oauth2 import service_account

        scopes = (
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/documents.readonly",
        )
        if self.credentials_json:
            try:
                info = (
                    json.loads(self.credentials_json)
                    if isinstance(self.credentials_json, str)
                    else self.credentials_json
                )
                return service_account.Credentials.from_service_account_info(
                    info,
                    scopes=scopes,
                )
            except Exception as error:
                logger.error("Failed to parse GOOGLE_SERVICE_ACCOUNT_JSON: %s", error)
        if self.credentials_path:
            return service_account.Credentials.from_service_account_file(
                self.credentials_path,
                scopes=scopes,
            )
        raise RuntimeError("No Google credentials provided (neither file nor JSON string).")

    def fetch_sheet_items(self, current_date: Optional[date] = None) -> list[AcademicItem]:
        from googleapiclient.discovery import build

        sheets = build(
            "sheets", "v4", credentials=self._credentials(), cache_discovery=False
        )
        response = execute_with_retry(
            lambda: sheets.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=self.sheet_range)
            .execute(),
            description="Google Sheets values.get",
        )
        return items_from_sheet_rows(response.get("values", []), current_date)

    def fetch_doc_items(self, current_date: Optional[date] = None) -> list[AcademicItem]:
        if not self.document_id:
            return []
        from googleapiclient.discovery import build

        docs = build("docs", "v1", credentials=self._credentials(), cache_discovery=False)

        def _get_document():
            try:
                return docs.documents().get(
                    documentId=self.document_id, includeTabsContent=True
                ).execute()
            except TypeError:
                return docs.documents().get(documentId=self.document_id).execute()

        document = execute_with_retry(
            _get_document,
            description="Google Docs documents.get",
        )
        return items_from_doc(document, current_date)

    def fetch_items(self, current_date: Optional[date] = None) -> list[AcademicItem]:
        """Fetch items from every configured source.

        A single failing source does not break the assistant: the remaining
        sources are still used. An error is raised only when no data could be
        loaded at all.
        """
        items: list[AcademicItem] = []
        errors: list[str] = []
        for source, fetch in (
            ("Google Sheet", self.fetch_sheet_items),
            ("Google Doc", self.fetch_doc_items),
        ):
            try:
                items.extend(fetch(current_date))
            except Exception as error:  # noqa: BLE001 - keep serving partial data
                errors.append(f"{source}: {error}")
                logger.error("Failed to load %s items: %s", source, error)
        if errors and not items:
            raise RuntimeError("Unable to load academic data. " + " | ".join(errors))
        if errors:
            logger.warning(
                "Continuing with partial academic data (%d items).", len(items)
            )
        return items


def format_daily_summary(items: list[AcademicItem], today: Optional[date] = None) -> str:
    today = today or date.today()
    tomorrow = today + timedelta(days=1)
    selected = [item for item in items if item.due_date in (today, tomorrow)]
    lines = [f"📚 Your academic update — {_format_date(today, '%A, %B')}"]
    for target, label in ((today, "Today"), (tomorrow, "Tomorrow")):
        due = [item for item in selected if item.due_date == target]
        lines.append(f"\n{label}:")
        if not due:
            lines.append("• No tasks or deadlines.")
            continue
        for item in due:
            description = f" — {item.details}" if item.details else ""
            lines.append(f"• [{item.category}] {item.title}{description} ({item.source})")
    return "\n".join(lines)


def _format_date(value: date, prefix: str) -> str:
    return f"{value.strftime(prefix)} {value.day}"


def days_until(target: Optional[date], today: Optional[date] = None) -> Optional[int]:
    """Number of days between ``today`` and ``target`` (negative when overdue)."""
    if target is None:
        return None
    today = today or date.today()
    return (target - today).days


def due_phrase(target: Optional[date], today: Optional[date] = None) -> str:
    """Human readable deadline wording such as ``Due tomorrow``."""
    remaining = days_until(target, today)
    if remaining is None:
        return "No due date"
    if remaining < 0:
        days = abs(remaining)
        return f"Overdue by {days} day{'s' if days != 1 else ''}"
    if remaining == 0:
        return "Due today"
    if remaining == 1:
        return "Due tomorrow"
    return f"Due in {remaining} days"


def due_icon(target: Optional[date], today: Optional[date] = None) -> str:
    remaining = days_until(target, today)
    if remaining is None:
        return "📄"
    if remaining < 0:
        return "🚨"
    if remaining == 0:
        return "🔥"
    if remaining == 1:
        return "⏰"
    return "📅"


def select_reminders(
    items: list[AcademicItem],
    today: Optional[date] = None,
    lead_days: int = REMINDER_LEAD_DAYS_DEFAULT,
    include_overdue: bool = True,
) -> list[AcademicItem]:
    """Items that are overdue or due within the next ``lead_days`` days."""
    today = today or date.today()
    horizon = today + timedelta(days=max(lead_days, 0))
    selected = []
    for item in items:
        if item.due_date is None:
            continue
        if include_overdue and item.due_date < today:
            selected.append(item)
        elif today <= item.due_date <= horizon:
            selected.append(item)
    selected.sort(key=lambda item: (item.due_date, item.category, item.title))
    return selected


def format_deadline_reminder(
    items: list[AcademicItem],
    today: Optional[date] = None,
    lead_days: int = REMINDER_LEAD_DAYS_DEFAULT,
) -> str:
    """Reminder message listing overdue and upcoming deadlines grouped by date."""
    today = today or date.today()
    reminders = select_reminders(items, today, lead_days)
    lines = [f"🔔 Deadline reminders — {_format_date(today, '%A, %B')}"]
    if not reminders:
        lines.append(
            f"\n✅ Nothing overdue and nothing due in the next "
            f"{lead_days} day{'s' if lead_days != 1 else ''}."
        )
        return "\n".join(lines)

    grouped: dict[date, list[AcademicItem]] = {}
    for item in reminders:
        grouped.setdefault(item.due_date, []).append(item)
    for due_date in sorted(grouped):
        lines.append(
            f"\n{due_icon(due_date, today)} {_format_date(due_date, '%A, %B')} "
            f"— {due_phrase(due_date, today)}"
        )
        for item in grouped[due_date]:
            description = f" — {item.details}" if item.details else ""
            lines.append(
                f"• [{item.category}] {item.title}{description} ({item.source})"
            )
    return "\n".join(lines)



GEMINI_CANDIDATE_MODELS = tuple(
    m.strip()
    for m in os.getenv(
        "GEMINI_MODELS",
        "gemini-3-flash-preview,gemini-3.5-flash,gemini-3.6-flash,gemini-3.1-flash-lite-preview",
    ).split(",")
    if m.strip()
)


def _request_gemini_candidate(
    model: str,
    url: str,
    payload: dict,
    timeout: int = 10,
) -> Optional[tuple[str, str]]:
    """Send a single Gemini API request. Returns (model_name, response_text) on success."""
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        if resp.status_code == 200:
            text = (
                resp.json()
                .get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
                .strip()
            )
            if text:
                return (model, text)
            logger.warning("Gemini candidate %s returned empty text.", model)
        elif resp.status_code == 400 and "thinkingConfig" in payload.get("generationConfig", {}):
            # Strip thinkingConfig in case this specific model does not support it
            retry_payload = json.loads(json.dumps(payload))
            retry_payload.get("generationConfig", {}).pop("thinkingConfig", None)
            retry_resp = requests.post(url, json=retry_payload, timeout=timeout)
            if retry_resp.status_code == 200:
                text = (
                    retry_resp.json()
                    .get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                    .strip()
                )
                if text:
                    return (model, text)
        else:
            logger.warning("Gemini model %s returned HTTP %d: %s", model, resp.status_code, resp.text[:120])
    except Exception as err:
        logger.warning("Gemini model %s request failed: %s", model, err)
    return None


def _race_gemini_models(
    models: Iterable[str],
    api_key: str,
    payload_builder: Callable[[str], dict],
    timeout: int = 4,
) -> Optional[tuple[str, str]]:
    """
    Execute candidate Gemini models with rapid failover to maximize speed while strictly conserving API quota.
    Supports comma-separated API keys in GEMINI_API_KEY for automatic quota rotation.
    """
    model_list = [m.strip() for m in models if m.strip()]
    if not model_list or not api_key:
        return None

    keys = [k.strip() for k in api_key.split(",") if k.strip()]
    t0 = time.time()

    for active_key in keys:
        for model in model_list:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={active_key}"
            payload = payload_builder(model)
            res = _request_gemini_candidate(model, url, payload, timeout=timeout)
            if res:
                elapsed = time.time() - t0
                logger.info(
                    "🏁 Gemini response from: %s in %.2fs (length: %d chars)",
                    res[0],
                    elapsed,
                    len(res[1]),
                )
                return res

    return None


def generate_ai_notification(
    notification_type: str,
    items: list[AcademicItem],
    today: date,
    lead_days: int = REMINDER_LEAD_DAYS_DEFAULT,
    api_key: Optional[str] = None,
) -> str:
    """Format morning summary or evening reminders into a warm, structured, headed Bengali message."""
    api_key = api_key or os.getenv("GEMINI_API_KEY")

    if notification_type == "morning":
        fallback = format_daily_summary(items, today)
    else:
        fallback = format_deadline_reminder(items, today, lead_days)

    if not api_key:
        return fallback

    items_text = "\n".join(
        f"- [{it.category}] {it.title} (Due: {it.due_date.strftime('%Y-%m-%d') if it.due_date else 'No date'})"
        for it in items
    ) or "No items recorded."

    time_label = "সকাল ৬:০০ টার রুটিন ও সারসংক্ষেপ" if notification_type == "morning" else "রাত ৮:০০ টার ডেডলাইন ও আগামীকালের রুটিন"
    prompt = (
        f"Today is {today.strftime('%A, %B %d, %Y')}.\n"
        f"You are the user's personal academic assistant. Generate a compact, standard-format {time_label} for Facebook Messenger in Bengali (বাংলা ভাষায়).\n\n"
        f"All tasks and routine from Google Sheets & Docs:\n{items_text}\n\n"
        "MANDATORY FORMAT & CONCISENESS RULES:\n"
        "1. STRICT CONCISENESS: Keep the entire message crisp, compact, and under 80 words. NO long intros, NO study tips, NO verbose fluff.\n"
        "2. STANDARD STRUCTURE:\n"
        "   - Top bold heading (e.g. '**🌅 আজকের রুটিন ও কাজ**' for morning, or '**🌙 আগামীকালের রুটিন ও ডেডলাইন**' for evening).\n"
        "   - Clean bullet points: '• [সময়/বিষয়]: [ক্লাস/রুম/কাজ]'.\n"
        "   - If there is an upcoming next-day task, list in 1-2 bullet points under '**📌 পরবর্তী কাজ:**'.\n"
        "   - If no tasks exist, state in 1 short line: '• কোনো ক্লাস বা নির্ধারিত ডেডলাইন নেই।'\n"
        "   - 1 short closing line: 'শুভ সকাল! 🌟' or 'শুভ রাত্রি! ✨'.\n"
        "3. Messenger formatting: Use bold '**Title**' and bullets '•'. Do NOT use '#' headers or '---' dividers."
    )

    def _build_notification_payload(model_name: str) -> dict:
        return {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.4,
                "maxOutputTokens": 300,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }

    winner = _race_gemini_models(
        GEMINI_CANDIDATE_MODELS, api_key, _build_notification_payload, timeout=6
    )
    if winner:
        return winner[1]

    return fallback


def _requested_date(question: str, today: date) -> Optional[date]:
    lowered = question.lower()
    if "day after tomorrow" in lowered:
        return today + timedelta(days=2)
    if "tomorrow" in lowered:
        return today + timedelta(days=1)
    if "today" in lowered:
        return today
    dates = _dates_in_text(question, today)
    return dates[0] if dates else None


MEMORY_FILE = os.path.join(os.path.dirname(__file__), "chat_history.json")
USER_MEMORY_FILE = os.path.join(os.path.dirname(__file__), "user_memory.json")
_memory_lock = Lock()
_user_memory_lock = Lock()
_MAX_HISTORY_TURNS = 20  # keeps last 10 exchanges (10 user + 10 model)

DEFAULT_PROFILES = {
    "28808065895499807": {
        "name": "তুহিন (Tuhin)",
        "department": "CSE",
        "notes": [
            "নাম তুহিন",
            "ডিপার্টমেন্ট CSE",
            "CSE বনাম Textile ক্রিকেট ম্যাচ খেলে",
            "রোল নম্বর 514250",
        ],
    }
}


def load_user_profile(user_id: Optional[str]) -> dict:
    if not user_id:
        return {}
    uid = str(user_id)
    with _user_memory_lock:
        data = {}
        if os.path.isfile(USER_MEMORY_FILE):
            try:
                with open(USER_MEMORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.warning("Failed to load user profile: %s", e)
        if uid not in data and uid in DEFAULT_PROFILES:
            data[uid] = DEFAULT_PROFILES[uid]
            save_user_profile(uid, data[uid])
        return data.get(uid, DEFAULT_PROFILES.get(uid, {}))


def save_user_profile(user_id: Optional[str], profile: dict) -> None:
    if not user_id:
        return
    uid = str(user_id)
    with _user_memory_lock:
        data = {}
        if os.path.isfile(USER_MEMORY_FILE):
            try:
                with open(USER_MEMORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[uid] = profile
        try:
            with open(USER_MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("Failed to save user profile: %s", e)


def remember_user_fact(user_id: Optional[str], text: str) -> None:
    """Extract and remember user facts (name, preferences, custom notes)."""
    if not user_id or not text:
        return
    profile = load_user_profile(user_id)
    cleaned = text.strip()
    lowered = cleaned.lower()

    # Name extraction: 'amar nam [X]', 'my name is [X]'
    name_match = re.search(r"\b(?:amar|amr)\s*nam(?:\s*ki\b)?\s*[:=]?\s*([A-Za-z\u0980-\u09FF\s]+)", cleaned, re.IGNORECASE)
    if not name_match:
        name_match = re.search(r"\bmy\s*name\s*is\s*([A-Za-z\u0980-\u09FF\s]+)", cleaned, re.IGNORECASE)
    if name_match and "ki" not in lowered.split()[:3]:
        extracted = name_match.group(1).strip().strip("!?. ")
        if extracted and len(extracted) < 40 and not any(w in extracted.lower() for w in ["ki", "bolo", "bholcho", "jano", "bhalo"]):
            profile["name"] = extracted

    # Memory note: 'mone rakhba ...', 'remember that ...'
    rem_match = re.search(r"\b(?:mone|mon)\s*(?:rakhba|rekho|rakho)\s*(?:je|j)?\s*(.+)", cleaned, re.IGNORECASE)
    if not rem_match:
        rem_match = re.search(r"\bremember\s*(?:that)?\s*(.+)", cleaned, re.IGNORECASE)
    if rem_match:
        fact = rem_match.group(1).strip().strip("!?. ")
        if fact:
            notes = profile.setdefault("notes", [])
            if fact not in notes:
                notes.append(fact)

    save_user_profile(user_id, profile)


def load_user_history(user_id: Optional[str]) -> list[dict]:
    if not user_id:
        return []
    with _memory_lock:
        if not os.path.isfile(MEMORY_FILE):
            return []
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get(str(user_id), [])
        except Exception as e:
            logger.warning("Failed to load chat history: %s", e)
            return []


def append_user_history(user_id: Optional[str], user_text: str, model_text: str) -> None:
    if not user_id:
        return
    with _memory_lock:
        data = {}
        if os.path.isfile(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        user_list = data.setdefault(str(user_id), [])
        user_list.append({"role": "user", "text": user_text})
        user_list.append({"role": "model", "text": model_text})
        if len(user_list) > _MAX_HISTORY_TURNS:
            data[str(user_id)] = user_list[-_MAX_HISTORY_TURNS:]
        try:
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("Failed to save chat history: %s", e)


def _fetch_attachment_part(att: dict) -> Optional[dict]:
    """Fetch an attachment URL from Messenger and prepare an inlineData Gemini part."""
    att_type = att.get("type", "").lower()
    url = att.get("payload", {}).get("url")
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200 or not resp.content:
            return None
        raw_bytes = resp.content
        if len(raw_bytes) > 20 * 1024 * 1024:
            logger.warning("Attachment from %s too large (%d bytes); skipping.", url, len(raw_bytes))
            return None
        content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()

        mime_type = None
        if att_type == "image" or content_type.startswith("image/"):
            if "png" in content_type:
                mime_type = "image/png"
            elif "webp" in content_type:
                mime_type = "image/webp"
            elif "gif" in content_type:
                mime_type = "image/gif"
            else:
                mime_type = "image/jpeg"
        elif att_type == "audio" or content_type.startswith("audio/"):
            if "wav" in content_type:
                mime_type = "audio/wav"
            elif "ogg" in content_type:
                mime_type = "audio/ogg"
            elif "aac" in content_type:
                mime_type = "audio/aac"
            elif "mp3" in content_type or "mpeg" in content_type:
                mime_type = "audio/mp3"
            else:
                mime_type = "audio/mp4"
        elif att_type == "file" or content_type.startswith("application/pdf") or content_type.startswith("text/"):
            if "pdf" in content_type or url.lower().endswith(".pdf"):
                mime_type = "application/pdf"
            elif "csv" in content_type:
                mime_type = "text/csv"
            elif "text" in content_type:
                mime_type = "text/plain"
            else:
                mime_type = "application/pdf"

        if mime_type:
            b64_data = base64.b64encode(raw_bytes).decode("utf-8")
            return {
                "inlineData": {
                    "mimeType": mime_type,
                    "data": b64_data,
                }
            }
    except Exception as err:
        logger.warning("Failed to fetch attachment from %s: %s", url, err)
    return None


def _clean_repetitive_name(text: str, user_name: str, question: str = "") -> str:
    """Ensure bot speaks like a discreet, natural personal agent without repetitive name addressing."""
    if not text:
        return text
    is_name_query = bool(re.search(
        r"\b(?:amar|my)\s*nam\b|আমার\s*নাম|who\s*am\s*i|আমাকে\s*চেনো|amake\s*cheno",
        question,
        re.IGNORECASE,
    )) if question else False
    if is_name_query:
        return text

    clean_name = re.split(r"[\s(]", user_name)[0].strip() if user_name else "তুহিন"
    name_tokens = [re.escape(clean_name), "তুহিন", "tuhin"]
    name_regex_part = "|".join(set(t for t in name_tokens if t))

    cleaned = text
    # Strip leading "তুহিন, " or "তুহিন ভাই, " or "Tuhin, "
    cleaned = re.sub(
        rf"^(?:{name_regex_part})(?:\s*(?:ভাই|bhai|bhaiya|vai))?\s*[,:!–-]\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    # Strip "বলো তুহিন, " / "বলুন তুহিন, "
    cleaned = re.sub(
        rf"\b(?:বলো|বলুন|bolo)\s*(?:{name_regex_part})[,\s]*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    # Strip name followed by comma mid-sentence
    cleaned = re.sub(
        rf"\s*\b(?:{name_regex_part})(?:\s*(?:ভাই|bhai|bhaiya|vai))?[,]\s*",
        ", ",
        cleaned,
        flags=re.IGNORECASE,
    )
    # Strip ", তুহিন" in the middle or end
    cleaned = re.sub(
        rf",\s*(?:{name_regex_part})(?:\s*(?:ভাই|bhai|bhaiya|vai))?\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    # Strip trailing " তুহিন!" or " তুহিন."
    cleaned = re.sub(
        rf"\s+(?:{name_regex_part})(?:\s*(?:ভাই|bhai|bhaiya|vai))?([!?.]*)$",
        r"\1",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


def generate_groq_reply(
    question: str,
    items: list[AcademicItem],
    today: date,
    api_key: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[str]:
    """Fallback LLM engine using Groq (Llama 3.3 70B) for ultra-fast, unlimited conversation."""
    api_key = api_key or os.getenv("GROQ_API_KEY")
    if not api_key or not question:
        return None

    profile = load_user_profile(user_id)
    user_name = profile.get("name", "তুহিন")
    user_dept = profile.get("department", "CSE")
    user_notes = profile.get("notes", [])

    profile_text = f"- User's Name: {user_name}\n- Department: {user_dept}"
    if user_notes:
        profile_text += "\n- Remembered notes: " + ", ".join(user_notes)

    items_text = "\n".join(
        f"- [{it.category}] {it.title} (Due: {it.due_date})"
        for it in items
    ) or "No items recorded."

    system_prompt = (
        "You are an elite, highly intelligent, warm personal academic and daily AI assistant on Facebook Messenger.\n"
        f"Today is {today.strftime('%A, %B %d, %Y')}.\n\n"
        f"=== USER PROFILE ===\n{profile_text}\n\n"
        f"=== ACADEMIC DATA & ROUTINE ===\n{items_text}\n\n"
        "STRICT RULES:\n"
        "1. You are their personal AI agent. Answer ANY query (academic, routines, general knowledge, life, code, math, friendly chat).\n"
        "2. Do NOT repeatedly mention the user's name ('তুহিন') in regular conversation. Speak directly, politely, and respectfully.\n"
        "3. Reply in authentic Bengali script (বাংলা) if user writes in Bengali or Banglish. If English, reply in English.\n"
        "4. Be concise, direct, helpful, and polite."
    )

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        "temperature": 0.3,
        "max_tokens": 350,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=8)
        if resp.status_code == 200:
            txt = resp.json()["choices"][0]["message"]["content"].strip()
            if txt:
                return _clean_repetitive_name(txt, user_name, question)
        else:
            logger.warning("Groq API returned HTTP %d: %s", resp.status_code, resp.text[:120])
    except Exception as err:
        logger.warning("Groq API request failed: %s", err)
    return None


def generate_gemini_reply(
    question: str,
    items: list[AcademicItem],
    today: date,
    lead_days: int = REMINDER_LEAD_DAYS_DEFAULT,
    api_key: Optional[str] = None,
    user_id: Optional[str] = None,
    attachments: Optional[list] = None,
) -> Optional[str]:
    """Generate a multi-turn, language-adaptive response using Gemini AI, supporting audio, images and files."""
    api_key = api_key or os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    items_summary = []
    for it in items:
        due = it.due_date.strftime("%Y-%m-%d") if it.due_date else "No due date"
        items_summary.append(
            f"- [{it.category}] {it.title} (Due: {due}, Details: {it.details}, Source: {it.source})"
        )
    items_text = "\n".join(items_summary) if items_summary else "No tasks currently recorded."

    # Persistent user memory & profile
    remember_user_fact(user_id, question)
    profile = load_user_profile(user_id)
    user_name = profile.get("name", "তুহিন (Tuhin)" if str(user_id) == os.getenv("ACADEMIC_RECIPIENT_PSID") else "তুহিন")
    user_dept = profile.get("department", "CSE")
    user_notes = profile.get("notes", [])

    profile_text = f"- User's Name: {user_name}\n- Department: {user_dept}"
    if user_notes:
        profile_text += "\n- Remembered Facts & Memories:\n" + "\n".join(f"  • {n}" for n in user_notes)

    system_instruction = (
        "You are an empathetic, highly intelligent, warm personal academic and daily assistant talking to the user on Facebook Messenger.\n"
        f"Today's date is: {today.strftime('%A, %B %d, %Y')}.\n\n"
        f"=== PERMANENT USER PROFILE & MEMORY ===\n"
        f"{profile_text}\n\n"
        f"Current tasks, assignments, deadlines, and notes from Google Sheets & Docs:\n"
        f"{items_text}\n\n"
        "STRICT CORE RULES:\n"
        "1. PERSONAL AGENT PERSONA & ADDRESSING STYLE (CRITICAL - DO NOT SPAM USER'S NAME):\n"
        "   - You are their elite, dedicated personal AI assistant/agent (যেমন একজন সত্যিকারের বিশ্বস্ত ব্যক্তিগত সহকারী)।\n"
        "   - STRICT PROHIBITION ON REPEATING USER'S NAME: DO NOT repeatedly address the user by their name ('তুহিন')! NEVER start messages with 'তুহিন,' or sprinkle 'তুহিন' into regular conversation. It sounds robotic and irritating.\n"
        "   - Speak directly, smoothly, politely, and respectfully like an executive personal assistant (use respectful 'আপনি' / 'আপনার' or natural conversational flow: 'জি, সকাল ১০টায় Physics II ক্লাস আছে...', 'আপনার অ্যাসাইনমেন্ট জমা দেওয়ার শেষ তারিখ...', 'আজকের শিডিউলে...').\n"
        "   - ONLY mention their name if they specifically ask about identity, such as 'amar nam ki', 'who am i', or 'amake cheno'. In all other regular answers, NEVER utter their name.\n"
        "2. SILENT MEMORY & CONTINUITY (MANDATORY):\n"
        f"   - You silently know this user in depth (Name: {user_name}, Dept: {user_dept}, roll, preferences, and remembered notes).\n"
        "   - Maintain complete continuity across conversation turns without explicitly announcing it unless asked.\n"
        "3. LANGUAGE ADAPTATION (MANDATORY):\n"
        "   - If the user writes or speaks in English, reply ONLY in natural, fluent English.\n"
        "   - If the user writes or speaks in Banglish (e.g. 'kmn acho', 'ajke ki kaj ache') or Bengali, reply in authentic Bengali script (বাংলা ভাষায়).\n"
        "   - If the user writes in Bengali script (বাংলা), reply in Bengali script (বাংলা).\n"
        "4. PROMPTS & CUSTOM INSTRUCTIONS (MANDATORY):\n"
        "   - If the user gives a specific format or instruction (e.g. 'bullet point e bolo', 'point akare dao', 'translate to...', 'solve this problem', '1 sentence e dao'), ALWAYS strictly follow that prompt instruction above all else.\n"
        "5. EXACT TARGETED ANSWERING (MANDATORY - DO NOT OVER-ANSWER):\n"
        "   - Answer ONLY the exact specific thing the user asked about. DO NOT dump the entire document, other subjects, or the full day's routine.\n"
        "   - Specific time query (যেমন: '১০টায় কি ক্লাস' বা '10 AM class'): Answer ONLY what is scheduled at 10:00 (যেমন: 'সকাল ১০:০০ টায় PHY127 - Physics II ক্লাস আছে (রুম: B3202)।'). NEVER mention other times, other classes, or unrelated assignments.\n"
        "   - Specific subject or task query (যেমন: 'physics assignment kobe'): Answer ONLY about that specific task/subject.\n"
        "   - Full schedule / routine: ONLY provide multiple classes or the full day's routine if the user explicitly asks for 'সারাদিনের রুটিন', 'সব ক্লাস', 'আজকের সব কাজ', or a specific day's routine (যেমন: 'রবিবারের রুটিন').\n"
        "   - Casual chat & greetings: Keep within 1-2 polite, helpful lines like a real personal agent.\n"
        "   - Explanations, math, document/image questions, or voice queries:\n"
        "       * Give the direct, clear answer or solution first.\n"
        "       * Use at most 2-4 short bullet points if explaining steps.\n"
        "       * Strictly avoid unsolicited study advice, long lecturing, or filler conclusions.\n"
        "   - Messenger formatting: Never use '#' Markdown headers. Use bold '**Title**' and bullet points '•'.\n"
        "6. MULTIMODAL INPUTS (MANDATORY):\n"
        "   - Voice notes / audio: Listen carefully to what was spoken in any language and reply concisely.\n"
        "   - Photos, images & documents: Examine all visual elements, formulas, routines, or PDFs and answer accurately and directly."
    )



    history = load_user_history(user_id)
    contents = []
    expected_role = "user"
    for turn in history:
        role = turn.get("role", "user")
        text = turn.get("text", "").strip()
        if text and role == expected_role:
            contents.append({"role": role, "parts": [{"text": text}]})
            expected_role = "model" if expected_role == "user" else "user"

    if contents and contents[-1]["role"] == "user":
        contents.pop()

    user_parts = []
    has_audio = False
    has_image = False
    has_doc = False
    if attachments:
        for att in attachments:
            part = _fetch_attachment_part(att)
            if part:
                user_parts.append(part)
                mtype = part["inlineData"]["mimeType"]
                if mtype.startswith("audio/"):
                    has_audio = True
                elif mtype.startswith("image/"):
                    has_image = True
                else:
                    has_doc = True

    prompt_text = question.strip() if question else ""
    if not prompt_text:
        hints = []
        if has_audio:
            hints.append("Listen carefully to the user's voice message / audio note, understand everything they asked or said, and respond concisely.")
        if has_image:
            hints.append("Examine the photo/image carefully, read any text, math, handwriting, questions, or diagrams, and provide an accurate, concise response.")
        if has_doc:
            hints.append("Review this document/file carefully and assist the user concisely based on its content.")
        prompt_text = " ".join(hints) if hints else "Hello! How can I help you today?"

    user_parts.append({"text": prompt_text})
    contents.append({"role": "user", "parts": user_parts})

    def _build_chat_payload(model_name: str) -> dict:
        return {
            "system_instruction": {
                "parts": [{"text": system_instruction}]
            },
            "contents": contents,
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 350,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }

    winner = _race_gemini_models(
        GEMINI_CANDIDATE_MODELS, api_key, _build_chat_payload, timeout=4
    )
    if winner:
        winning_model, winning_text = winner
        winning_text = _clean_repetitive_name(winning_text, user_name, question)
        if user_id:
            history_question = question if question else ("[Voice message]" if has_audio else "[Photo/File]")
            append_user_history(user_id, history_question, winning_text)
        return winning_text

    return None


def conversational_reply(
    question: str,
    items: list[AcademicItem],
    today: date,
    lead_days: int = REMINDER_LEAD_DAYS_DEFAULT,
    user_id: Optional[str] = None,
) -> Optional[str]:
    """Smart built-in Bengali / Banglish conversational and schedule assistant."""
    lowered = question.lower().strip()
    clean = re.sub(r"[^\w\s\u0980-\u09FF]", " ", lowered)
    clean = re.sub(r"\s+", " ", clean).strip()

    # Load permanent user memory
    profile = load_user_profile(user_id)
    roll_num = profile.get("roll", "514250")
    for n in profile.get("notes", []):
        m_r = re.search(r"(\d{5,7})", n)
        if m_r:
            roll_num = m_r.group(1)
            break
    user_name = profile.get("name", "তুহিন")
    clean_user_name = re.split(r"[\s(]", user_name)[0].strip() or "তুহিন"
    dept_name = profile.get("department", "CSE")

    bn_days = {
        "Saturday": "শনিবার", "Sunday": "রবিবার", "Monday": "সোমবার",
        "Tuesday": "মঙ্গলবার", "Wednesday": "বুধবার", "Thursday": "বৃহস্পতিবার", "Friday": "শুক্রবার"
    }

    # 1. Roll number queries
    if re.search(r"\b(roll|rol)\b|রোল", clean):
        return f"আপনার রোল নম্বর হলো **{roll_num}**। 📝"

    # 2. Identity / Name queries
    if re.search(r"\b(amar|amr|my)\s*nam\b|আমার\s*নাম|who\s*am\s*i|আমাকে\s*চেনো|amake\s*cheno", clean):
        return f"আপনার নাম **{clean_user_name}**। আপনি {dept_name} ডিপার্টমেন্টের শিক্ষার্থী। 😊"

    # 3. Department queries
    if re.search(r"\b(dept|department)\b|ডিপার্টমেন্ট|বিভাগ", clean):
        return f"আপনার ডিপার্টমেন্ট হলো **{dept_name}**। 💻"

    # 4. Remember user fact
    if re.search(r"\b(?:mone|mon)\s*(?:rakhba|rekho|rakho)\b|\bremember\b", clean):
        remember_user_fact(user_id, question)
        return "অবশ্যই, আমি আপনার এই তথ্যটি মনে রেখেছি এবং সেভ করে নিয়েছি! 📝"

    # 5. Day of the week queries
    if re.search(r"\b(aj|ajke)\s*(ki\s*bar|kibar)\b|আজ\s*কী\s*বার|আজকে\s*কী\s*বার", clean):
        return f"আজকে হলো **{bn_days.get(today.strftime('%A'), today.strftime('%A'))}**। 🗓️"
    if re.search(r"\b(kal|kalke)\s*(ki\s*bar|kibar)\b|কাল\s*কী\s*বার|কালকে\s*কী\s*বার", clean):
        tom = today + timedelta(days=1)
        return f"আগামীকাল হলো **{bn_days.get(tom.strftime('%A'), tom.strftime('%A'))}**। 🗓️"

    # 6. Feelings & Casual Chat
    if re.search(r"\b(bhalo|valo)\s*lage\s*na\b|ভালো\s*লাগে\s*না|মন\s*খারাপ", clean):
        return "মন খারাপ করবেন না! একটু রিল্যাক্স করুন বা একটু হেঁটে আসুন। পড়ালেখা বা যেকোনো প্রয়োজনে আমি সবসময় আপনার পাশে আছি! 😊✨"

    if re.search(r"\b(ki\s*koro|ki\s*kortaso|ki\s*koren)\b|কী\s*কর", clean):
        return "আপনার ক্লাসের রুটিন ও শিডিউল নিয়ে প্রস্তুত আছি! আজকের বা কালকের কোনো আপডেট জানতে চান? 📚"

    if re.search(r"\b(kire|ki\s*khobor|ki\s*obostha|kmn|kemon|kemn|hi|hello|hey)\b|কেমন|কী\s*অবস্থা|কী\s*খবর", clean):
        return "আলহামদুলিল্লাহ, আমি খুব ভালো আছি! 😊 আপনার পড়াশোনা বা ক্লাসের কী অবস্থা? আজকের কোনো আপডেট বা শিডিউল জানতে চান?"

    if re.search(r"\b(salam|assalam|assalamu|assalamualaikum|slaam)\b|সালাম|আসসালামু\s*আলাইকুম", clean):
        return "ওয়ালাইকুমুস সালাম ওয়া রাহমাতুল্লাহ! কেমন আছেন? আমি আপনার পার্সোনাল অ্যাসিস্ট্যান্ট। আজ আপনাকে কীভাবে সাহায্য করতে পারি?"

    if re.search(r"\b(dhonnobad|dhonno|thanks|thank\s*you|thx)\b|ধন্যবাদ", clean):
        return "আপনাকে অনেক ধন্যবাদ! 😊 যেকোনো প্রয়োজনে আমাকে নক দিতে পারেন, আমি সবসময় প্রস্তুত আছি।"

    if re.search(r"\b(alhamdulillah|alhamdullilah)\b|আলহামদুলিল্লাহ", clean):
        return "মাশাআল্লাহ! শুনে খুব ভালো লাগলো। আজকের কোনো কাজ বা পড়ালেখা সম্পর্কে কিছু জানতে চান?"

    if re.search(r"\b(tumi|apni)\s*(ke|k)\b|\b(ki\s*korte\s*paro|tumi\s*ki|tumar\s*kaj\s*ki)\b|who\s*are\s*you|who\s*r\s*u|তুমি\s*কে|আপনি\s*কে|কী\s*করতে\s*পার|তোমার\s*কাজ\s*কী", clean):
        return "আমি আপনার সার্বক্ষণিক পার্সোনাল অ্যাকাডেমিক এজেন্ট! 🤖 আপনার ক্লাস রুটিন মনে রাখা, অ্যাসাইনমেন্ট ও ডেডলাইন ট্র্যাকিং এবং পড়ালেখার যেকোনো সহায়তায় সাহায্য করাই আমার কাজ।"

    # 7. Routine & Class Queries
    has_class_word = bool(re.search(r"\b(class|routine|rutin|schedule|shodul|porashona|subject|sob|shob)\b|ক্লাস|রুটিন|পড়ালেখা|বিষয়", clean))

    weekday_map = {
        "shonibar": ("Saturday", "শনিবার"), "shoni": ("Saturday", "শনিবার"), "saturday": ("Saturday", "শনিবার"), "শনিবার": ("Saturday", "শনিবার"),
        "robibar": ("Sunday", "রবিবার"), "robi": ("Sunday", "রবিবার"), "sunday": ("Sunday", "রবিবার"), "রবিবার": ("Sunday", "রবিবার"),
        "sombar": ("Monday", "সোমবার"), "som": ("Monday", "সোমবার"), "monday": ("Monday", "সোমবার"), "সোমবার": ("Monday", "সোমবার"),
        "mongolbar": ("Tuesday", "মঙ্গলবার"), "mongol": ("Tuesday", "মঙ্গলবার"), "tuesday": ("Tuesday", "মঙ্গলবার"), "মঙ্গলবার": ("Tuesday", "মঙ্গলবার"),
        "budhbar": ("Wednesday", "বুধবার"), "budh": ("Wednesday", "বুধবার"), "wednesday": ("Wednesday", "বুধবার"), "বুধবার": ("Wednesday", "বুধবার"),
        "brihospotibar": ("Thursday", "বৃহস্পতিবার"), "brihospoti": ("Thursday", "বৃহস্পতিবার"), "thursday": ("Thursday", "বৃহস্পতিবার"), "বৃহস্পতিবার": ("Thursday", "বৃহস্পতিবার"),
        "shukrobar": ("Friday", "শুক্রবার"), "shukro": ("Friday", "শুক্রবার"), "friday": ("Friday", "শুক্রবার"), "শুক্রবার": ("Friday", "শুক্রবার"),
    }

    target_day_en = None
    target_day_bn = None

    if re.search(r"\b(aj|ajke|ajker|today)\b|আজ|আজকে|আজকের", clean):
        target_day_en = today.strftime("%A")
        target_day_bn = "আজকের (" + bn_days.get(target_day_en, target_day_en) + ")"
    elif re.search(r"\b(kal|kalke|kalker|tomorrow)\b|কাল|কালকে|কালকের|আগামীকাল", clean):
        tom = today + timedelta(days=1)
        target_day_en = tom.strftime("%A")
        target_day_bn = "আগামীকালকের (" + bn_days.get(target_day_en, target_day_en) + ")"
    elif re.search(r"\b(porsho|porshu)\b|পরশু", clean):
        porsh = today + timedelta(days=2)
        target_day_en = porsh.strftime("%A")
        target_day_bn = "পরশুর (" + bn_days.get(target_day_en, target_day_en) + ")"
    else:
        for w_key, (d_en, d_bn) in weekday_map.items():
            if re.search(rf"\b{w_key}\b|{w_key}", clean):
                target_day_en = d_en
                target_day_bn = f"{d_bn}ের"
                break

    # If asking for class/routine on a day
    if target_day_en and (has_class_word or "ki" in clean or "কী" in clean):
        day_classes = [
            it for it in items
            if it.due_date and it.due_date.strftime("%A").lower() == target_day_en.lower()
            and ("room" in it.title.lower() or any(t in it.title for t in ["10:00", "11:30", "13:00"]))
        ]
        if day_classes:
            lines = [f"📅 **{target_day_bn} ক্লাসের শিডিউল:**"]
            for it in day_classes:
                lines.append(f"• {it.title}")
            return "\n".join(lines)
        else:
            return f"{target_day_bn} আপনার কোনো ক্লাস নির্ধারিত নেই! রিলাক্স থাকতে পারেন। 😊"

    # Specific time class inquiry: "10tay ki class" / "11:30 te ki"
    time_match = re.search(r"\b(10[:.]00|10|11[:.]30|11|1[:.]00|1|13[:.]00|13)\s*(?:tay|am|pm|টার|টায়)?\b", clean)
    if time_match and (has_class_word or "ki" in clean or "room" in clean):
        prefix = time_match.group(1)
        target_time = "10:00" if "10" in prefix else ("11:30" if "11" in prefix else "13:00")
        today_en = today.strftime("%A")
        time_matches = [
            it for it in items
            if it.due_date and it.due_date.strftime("%A").lower() == today_en.lower()
            and target_time in it.title
        ]
        if time_matches:
            return f"আজকের শিডিউল অনুযায়ী সকাল {target_time} টায় আপনার ক্লাস:\n• {time_matches[0].title} 😊"
        else:
            return f"আজকে {target_time} টায় কোনো ক্লাস নির্ধারিত নেই।"

    # 8. Bengali / Banglish task inquiries
    has_task_word = bool(re.search(r"\b(kaj|kaje|task|korte\s*hobe|ki\s*korbo|bad|baki|shesh|deadline)\b|কাজ|করতে\s*হবে|কী\s*করব|বাকি|বাদ|ডেডলাইন", clean))
    has_missed = bool(re.search(r"\b(bad|baki|overdue|miss|missed)\b|বাকি|বাদ|ছুটে|ওভারডিউ", clean))

    if (has_task_word or has_missed) and target_day_en:
        target_date = today if "আজ" in (target_day_bn or "") else (today + timedelta(days=1))
        matching_tasks = [it for it in items if it.due_date == target_date]
        if matching_tasks:
            lines = [f"📋 {target_day_bn} কাজের আপডেট:"]
            for it in matching_tasks:
                det = f" — {it.details}" if it.details else ""
                lines.append(f"• [{it.category}] {it.title}{det}")
            return "\n".join(lines)
        else:
            return f"আলহামদুলিল্লাহ, {target_day_bn} কোনো অ্যাসাইনমেন্ট বা ডেডলাইনের চাপ নেই! 🎉"

    return None

def answer_question(
    question: str,
    items: list[AcademicItem],
    today: Optional[date] = None,
    lead_days: int = REMINDER_LEAD_DAYS_DEFAULT,
    user_id: Optional[str] = None,
    attachments: Optional[list] = None,
) -> str:
    today = today or date.today()
    items = list(items)
    command = question.strip().lower().strip("!?. ")
    if command in {"help", "commands", "menu"} and not attachments:
        return HELP_TEXT

    # 1. AI reply with Gemini
    ai_reply = generate_gemini_reply(
        question, items, today, lead_days, user_id=user_id, attachments=attachments
    )
    # 2. Fallback AI with Groq if Gemini hits 429 quota or fails
    if not ai_reply and not attachments and question:
        ai_reply = generate_groq_reply(question, items, today, user_id=user_id)

    if ai_reply:
        return ai_reply

    if not question and attachments:
        return "আমি আপনার পাঠানো ফাইল/ভয়েস মেসেজটি পেয়েছি, কিন্তু এই মুহূর্তে এআই প্রসেস করতে পারছে না। দয়া করে একটু পর আবার চেষ্টা করুন বা লিখে জানান! 😊"

    # 3. Offline fallback for help greetings
    if command in GREETING_WORDS:
        return HELP_TEXT

    # 4. Reminder keywords
    if command in REMINDER_WORDS:
        return format_deadline_reminder(items, today, lead_days)

    # 5. Smart Bengali / Banglish conversational responder
    conv_reply = conversational_reply(question, items, today, lead_days, user_id=user_id)
    if conv_reply:
        return conv_reply

    # 6. Date-based query matching
    requested_date = _requested_date(question, today)
    if requested_date:
        matches = [item for item in items if item.due_date == requested_date]
        if "assignment" in question.lower():
            assignments = [
                item
                for item in matches
                if "assignment" in item.searchable_text.lower()
                or "homework" in item.searchable_text.lower()
            ]
            if assignments:
                matches = assignments
        if not matches:
            return (
                f"{_format_date(requested_date, '%A, %B')} তারিখে আপনার কোনো নির্ধারিত টাস্ক বা ক্লাস পাওয়া যায়নি।"
            )
        return (
            f"📅 {_format_date(requested_date, '%A, %B')} তারিখের তথ্য:\n"
            + "\n".join(
                f"• [{item.category}] {item.title}"
                + (f" — {item.details}" if item.details else "")
                + f" ({item.source})"
                for item in matches
            )
        )

    # 7. Targeted academic keyword matching
    terms = {
        term
        for term in re.findall(r"[a-z0-9]+", question.lower())
        if term not in {"what", "is", "my", "the", "for", "about", "tell", "me", "please", "kire", "ki", "kmn", "kemon", "koto"}
    }
    ranked = []
    for item in items:
        text = item.searchable_text.lower()
        score = sum(bool(re.search(rf"\b{re.escape(term)}\b", text)) for term in terms)
        if score:
            ranked.append((score, item))
    ranked.sort(key=lambda entry: entry[0], reverse=True)
    if ranked:
        return "📌 আপনার অ্যাকাডেমিক তথ্য:\n" + "\n".join(
            f"• {item.title}"
            + (f" — {item.details}" if item.details else "")
            + (f" ({_format_date(item.due_date, '%b')})" if item.due_date else "")
            for _, item in ranked[:3]
        )

    # 8. Friendly, transparent agent fallback
    return (
        "আমি আপনার সাথে একজন সার্বক্ষণিক পার্সোনাল সহকারী হিসেবে যুক্ত আছি! 🤖 "
        "এই মুহূর্তে মুক্ত এআই চ্যাটের জন্য গুগলের ফ্রি এপিআই কোটা সাময়িকভাবে শেষ হয়েছে। "
        "তবে আপনার সকল ক্লাস রুটিন, শিডিউল, রোল ও অ্যাকাডেমিক তথ্য সবসময় শতভাগ সচল আছে! "
        "আজকের রুটিন, কালকের ক্লাস বা যেকোনো শিডিউল জানতে এখনই লিখে জানাতে পারেন! 😊"
    )

def repository_from_environment() -> GoogleAcademicRepository:
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
    sheet_range = os.getenv("GOOGLE_SHEETS_RANGE")
    if not spreadsheet_id or not sheet_range:
        raise RuntimeError(
            "Missing required Google configuration: GOOGLE_SHEETS_SPREADSHEET_ID and GOOGLE_SHEETS_RANGE must be set."
        )
    credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    credentials_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not credentials_path and not credentials_json:
        raise RuntimeError(
            "Missing required Google configuration: GOOGLE_APPLICATION_CREDENTIALS or GOOGLE_SERVICE_ACCOUNT_JSON must be set."
        )
    document_id = os.getenv("GOOGLE_DOC_ID") or None
    if not document_id:
        logger.warning("GOOGLE_DOC_ID is not set; only the Google Sheet will be read.")
    return GoogleAcademicRepository(
        spreadsheet_id=spreadsheet_id,
        sheet_range=sheet_range,
        document_id=document_id,
        credentials_path=credentials_path,
        credentials_json=credentials_json,
    )
