import base64
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
                parsed = datetime.strptime(target_val, date_format).date()
                if "%Y" not in date_format and "%y" not in date_format:
                    parsed = parsed.replace(year=current_date.year)
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

    models = (
        "gemini-flash-lite-latest",
        "gemini-3.1-flash-lite-preview",
        "gemini-flash-latest",
    )
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 300},
        }
        try:
            resp = requests.post(url, json=payload, timeout=20)
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
                    return text
        except Exception as e:
            logger.warning("AI notification generation failed with %s: %s", model, e)
            continue

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
_memory_lock = Lock()
_MAX_HISTORY_TURNS = 16  # keeps last 8 exchanges (8 user + 8 model)


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

    system_instruction = (
        "You are an empathetic, highly intelligent, warm personal academic and daily assistant talking to the user on Facebook Messenger.\n"
        f"Today's date is: {today.strftime('%A, %B %d, %Y')}.\n\n"
        f"Current tasks, assignments, deadlines, and notes from Google Sheets & Docs:\n"
        f"{items_text}\n\n"
        "STRICT CORE RULES:\n"
        "1. MEMORY & CONTEXT:\n"
        "   - You remember past conversation turns from this chat. Seamlessly reference past context when appropriate.\n"
        "2. LANGUAGE ADAPTATION (MANDATORY):\n"
        "   - If the user writes or speaks in English, reply ONLY in natural, fluent English.\n"
        "   - If the user writes or speaks in Banglish (e.g. 'kmn acho', 'ajke ki kaj ache') or Bengali, reply in authentic Bengali script (বাংলা ভাষায়).\n"
        "   - If the user writes in Bengali script (বাংলা), reply in Bengali script (বাংলা).\n"
        "3. PROMPTS & CUSTOM INSTRUCTIONS (MANDATORY):\n"
        "   - If the user gives a specific format or instruction (e.g. 'bullet point e bolo', 'point akare dao', 'translate to...', 'solve this problem', '1 sentence e dao'), ALWAYS strictly follow that prompt instruction above all else.\n"
        "4. EXACT TARGETED ANSWERING (MANDATORY - DO NOT OVER-ANSWER):\n"
        "   - Answer ONLY the exact specific thing the user asked about. DO NOT dump the entire document, other subjects, or the full day's routine.\n"
        "   - Specific time query (যেমন: '১০টায় কি ক্লাস' বা '10 AM class'): Answer ONLY what is scheduled at 10:00 (যেমন: 'সকাল ১০:০০ টায় PHY127 - Physics II ক্লাস আছে (রুম: B3202)।'). NEVER mention other times, other classes, or unrelated assignments.\n"
        "   - Specific subject or task query (যেমন: 'physics assignment kobe'): Answer ONLY about that specific task/subject.\n"
        "   - Full schedule / routine: ONLY provide multiple classes or the full day's routine if the user explicitly asks for 'সারাদিনের রুটিন', 'সব ক্লাস', 'আজকের সব কাজ', or a specific day's routine (যেমন: 'রবিবারের রুটিন').\n"
        "   - Casual chat & greetings: Keep within 1-2 friendly lines.\n"
        "   - Explanations, math, document/image questions, or voice queries:\n"
        "       * Give the direct, clear answer or solution first.\n"
        "       * Use at most 2-4 short bullet points if explaining steps.\n"
        "       * Strictly avoid unsolicited study advice, long lecturing, or filler conclusions.\n"
        "   - Messenger formatting: Never use '#' Markdown headers. Use bold '**Title**' and bullet points '•'.\n"
        "5. MULTIMODAL INPUTS (MANDATORY):\n"
        "   - Voice notes / audio: Listen carefully to what was spoken in any language and reply concisely.\n"
        "   - Photos, images & documents: Examine all visual elements, formulas, routines, or PDFs and answer accurately and directly."
    )

    models = (
        "gemini-flash-lite-latest",
        "gemini-3.1-flash-lite-preview",
        "gemini-flash-latest",
    )

    history = load_user_history(user_id)
    contents = []
    for turn in history:
        role = turn.get("role", "user")
        text = turn.get("text", "")
        if text:
            contents.append({"role": role, "parts": [{"text": text}]})

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

    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "system_instruction": {
                "parts": [{"text": system_instruction}]
            },
            "contents": contents,
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 300,
            },
        }
        try:
            resp = requests.post(url, json=payload, timeout=25)
            if resp.status_code == 200:
                data = resp.json()
                text = (
                    data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                    .strip()
                )
                if text:
                    if user_id:
                        history_question = question if question else ("[Voice message]" if has_audio else "[Photo/File]")
                        append_user_history(user_id, history_question, text)
                    return text
            else:
                logger.warning("Gemini API %s error %d: %s", model, resp.status_code, resp.text[:150])
                continue
        except Exception as err:
            logger.warning("Gemini API call failed for %s: %s", model, err)
            continue
    return None


def conversational_reply(
    question: str,
    items: list[AcademicItem],
    today: date,
    lead_days: int = REMINDER_LEAD_DAYS_DEFAULT,
) -> Optional[str]:
    """Smart built-in Bengali / Banglish conversational responder."""
    lowered = question.lower().strip()
    clean = re.sub(r"[^\w\s\u0980-\u09FF]", " ", lowered)
    clean = re.sub(r"\s+", " ", clean).strip()

    # 1. Greetings & Well-being
    if re.search(r"\b(kemon|kmn|kemn)\s*(acho|asen|aso|achen|acis|asis)\b|\b(valo|bhalo)\s*(acho|aso|asen|achen)\b|কেমন\s*(আছো|আছেন|আছিস)|ভালো\s*(আছো|আছেন)|how\s+are\s+you|how\s+r\s+u", clean):
        return "আলহামদুলিল্লাহ, আমি খুব ভালো আছি! 😊 আপনি কেমন আছেন? আজকের কাজ বা কোনো শিডিউলের খোঁজ নিবেন?"

    if re.search(r"\b(ki|kee)\s*(obostha|khobor|khbr)\b|কী\s*(অবস্থা|খবর)|whats?\s*up", clean):
        return "আলহামদুলিল্লাহ, সব ঠিকঠাক চলছে! আপনার পড়াশোনা ও কাজের কী অবস্থা? আজকের কোনো আপডেট জানতে চান?"

    if re.search(r"\b(salam|assalam|assalamu|assalamualaikum|slaam)\b|সালাম|আসসালামু\s*আলাইকুম", clean):
        return "ওয়ালাইকুমুস সালাম ওয়া রাহমাতুল্লাহ! কেমন আছেন? আমি আপনার পার্সোনাল অ্যাসিস্ট্যান্ট। আজ আপনাকে কীভাবে সাহায্য করতে পারি?"

    if re.search(r"\b(dhonnobad|dhonno|thanks|thank\s*you|thx)\b|ধন্যবাদ", clean):
        return "আপনাকে অনেক ধন্যবাদ! 😊 যেকোনো প্রয়োজনে আমাকে নক দিতে পারেন, আমি সবসময় প্রস্তুত আছি।"

    if re.search(r"\b(alhamdulillah|alhamdullilah)\b|আলহামদুলিল্লাহ", clean):
        return "মাশাআল্লাহ! শুনে খুব ভালো লাগলো। আজকের কোনো কাজ বা পড়ালেখা সম্পর্কে কিছু জানতে চান?"

    if re.search(r"\b(tumi|apni)\s*(ke|k)\b|who\s*are\s*you|who\s*r\s*u|তুমি\s*কে|আপনি\s*কে", clean):
        return "আমি আপনার পার্সোনাল একাডেমিক অ্যাসিস্ট্যান্ট! 🤖 আপনার Google Sheet এবং Doc দেখে প্রতিদিনের কাজ, অ্যাসাইনমেন্ট ও ডেডলাইন মনে করিয়ে দেওয়াই আমার কাজ।"

    # 2. Bengali / Banglish task inquiries
    has_today = bool(re.search(r"\b(aj|ajke|ajker)\b|আজ|আজকে|আজকের", clean))
    has_tomorrow = bool(re.search(r"\b(kal|kalke|kalker)\b|কাল|কালকে|কালকের|আগামীকাল", clean))
    has_task_word = bool(re.search(r"\b(kaj|kaje|task|korte\s*hobe|ki\s*korbo|bad|baki|shesh|deadline)\b|কাজ|করতে\s*হবে|কী\s*করব|বাকি|বাদ|ডেডলাইন", clean))
    has_missed = bool(re.search(r"\b(bad|baki|overdue|miss|missed)\b|বাকি|বাদ|ছুটে|ওভারডিউ", clean))

    # Questions like: "ajke ki kono kaj bad ache ba ki korte hobe" / "ajke ki kaj ache"
    if has_today and (has_task_word or has_missed):
        overdue = [it for it in items if it.due_date and it.due_date < today]
        today_items = [it for it in items if it.due_date == today]
        undated = [it for it in items if not it.due_date]

        if overdue or today_items:
            lines = ["📋 আজকের কাজের আপডেট:"]
            if overdue:
                lines.append("\n⚠️ আগের জমে থাকা/ওভারডিউ কাজ:")
                for it in overdue:
                    lines.append(f"• [{it.category}] {it.title} ({due_phrase(it.due_date, today)})")
            if today_items:
                lines.append("\n🔥 আজকের নির্ধারিত কাজ:")
                for it in today_items:
                    det = f" — {it.details}" if it.details else ""
                    lines.append(f"• [{it.category}] {it.title}{det}")
            lines.append("\nসময়মতো শেষ করে ফেলুন, শুভকামনা! 💪")
            return "\n".join(lines)
        else:
            if undated:
                undated_summary = "\n".join(f"• {it.title}" for it in undated[:4])
                return (
                    f"আলহামদুলিল্লাহ! আজকে আপনার কোনো নির্দিষ্ট ডেডলাইন বাকি নেই। 🎉\n\n"
                    f"📌 তবে আপনার ডকুমেন্টে এই সাধারণ নোট/কাজগুলো আছে:\n{undated_summary}\n\n"
                    f"আগামীকালের কাজ দেখতে 'কালকের কাজ' বা 'reminders' লিখে দেখতে পারেন।"
                )
            return (
                "আলহামদুলিল্লাহ! আজকে আপনার কোনো কাজ বা ডেডলাইন বাকি নেই, সব ক্লিয়ার! 🎉\n"
                "সামনের ডেডলাইনগুলো দেখতে চাইলে 'reminders' লিখে দেখতে পারেন।"
            )

    # Questions about tomorrow in Bengali / Banglish:
    if has_tomorrow and has_task_word:
        tomorrow = today + timedelta(days=1)
        tomorrow_items = [it for it in items if it.due_date == tomorrow]
        if tomorrow_items:
            lines = [f"⏰ আগামীকালের নির্ধারিত কাজ ({_format_date(tomorrow, '%A')}):"]
            for it in tomorrow_items:
                det = f" — {it.details}" if it.details else ""
                lines.append(f"• [{it.category}] {it.title}{det}")
            lines.append("\nআগে থেকেই একটু গুছিয়ে রাখুন যাতে কালকে প্রেশার না হয়! 😊")
            return "\n".join(lines)
        else:
            return "আগামীকালের জন্য কোনো নির্ধারিত কাজ বা ডেডলাইন নেই! নিশ্চিন্তে থাকতে পারেন। 😊"

    # Questions specifically asking about missed / overdue / remaining tasks
    if has_missed and (has_task_word or "kaj" in clean or "কাজ" in clean):
        overdue = [it for it in items if it.due_date and it.due_date < today]
        if overdue:
            lines = ["🚨 আপনার নিচের কাজগুলোর ডেডলাইন পার হয়ে গেছে (Overdue):"]
            for it in overdue:
                lines.append(f"• [{it.category}] {it.title} — {due_phrase(it.due_date, today)}")
            lines.append("\nযত দ্রুত সম্ভব এগুলো শেষ করে ফেলুন!")
            return "\n".join(lines)
        else:
            return "আলহামদুলিল্লাহ, আপনার পেছনের কোনো কাজ ওভারডিউ বা বাকি নেই! সব আপ-টু-ডেট আছে। 👏"

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

    # 1. AI reply with Gemini (supports text, voice audio, images, files)
    ai_reply = generate_gemini_reply(
        question, items, today, lead_days, user_id=user_id, attachments=attachments
    )
    if ai_reply:
        return ai_reply

    if not question and attachments:
        return "আমি আপনার পাঠানো ফাইল/ভয়েস মেসেজটি পেয়েছি, কিন্তু এই মুহূর্তে এআই প্রসেস করতে পারছে না। দয়া করে একটু পর আবার চেষ্টা করুন বা লিখে জানান! 😊"

    # 2. Offline fallback for help greetings
    if command in GREETING_WORDS:
        return HELP_TEXT

    # 3. Reminder keywords
    if command in REMINDER_WORDS:
        return format_deadline_reminder(items, today, lead_days)

    # 2. Smart Bengali / Banglish natural conversation helper
    conv_reply = conversational_reply(question, items, today, lead_days)
    if conv_reply:
        return conv_reply

    # 3. Date-based query matching
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
                f"I couldn't find any tasks dated "
                f"{_format_date(requested_date, '%B')}, {requested_date:%Y}."
            )
        return (
            f"Here’s what I found for {_format_date(requested_date, '%A, %B')}, "
            f"{requested_date:%Y}:\n"
            + "\n".join(
                f"• [{item.category}] {item.title}"
                + (f" — {item.details}" if item.details else "")
                + f" ({item.source})"
                for item in matches
            )
        )

    terms = {
        term
        for term in re.findall(r"[a-z0-9]+", question.lower())
        if term not in {"what", "is", "my", "the", "for", "about", "tell", "me", "please"}
    }
    ranked = []
    for item in items:
        text = item.searchable_text.lower()
        score = sum(bool(re.search(rf"\b{re.escape(term)}\b", text)) for term in terms)
        if score:
            ranked.append((score, item))
    ranked.sort(key=lambda entry: entry[0], reverse=True)
    if not ranked:
        return "I couldn't find matching information in your Google Sheet or Doc."
    return "Here’s the relevant academic information I found:\n" + "\n".join(
        f"• {item.title}"
        + (f" — {item.details}" if item.details else "")
        + (f" (due {_format_date(item.due_date, '%b')})" if item.due_date else "")
        + f" ({item.source})"
        for _, item in ranked[:5]
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
