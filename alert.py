from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import smtplib
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import random

import requests
import schedule
from bs4 import BeautifulSoup
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
IST = ZoneInfo("Asia/Kolkata")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


CONFIG = {
    "EMAIL_SENDER": os.getenv("EMAIL_USER", "").strip(),
    "EMAIL_PASSWORD": os.getenv("EMAIL_PASSWORD", "").strip(),
    "EMAIL_TO": os.getenv("EMAIL_TO", "").strip(),

    "SEARCH_QUERIES": [

        "machine learning engineer",
        "machine learning intern",
        "AI engineer",
        "AI ML engineer",
        # "data scientist",
        # "data science intern",
        # "deep learning engineer",
        # "computer vision engineer",
        # "NLP engineer",
        # "LLM engineer",
        # "Generative AI engineer",
        # "AI agent developer",
        # "RAG engineer",
        # "applied scientist",
        # "research engineer AI",

    ],

    "LOCATIONS": [
        "India",
        # "Remote",
        # "Bangalore",
        # "Bengaluru",
        # "Hyderabad",
        # "Pune",
        # "Chennai",
        # "Noida",
        # "Gurgaon",
        # "Gurugram",
        # "Delhi",
        # "New Delhi",
        # "Mumbai",
        # "Navi Mumbai",
        # "Kolkata",
        # "Ahmedabad",
        # "Jaipur",
        # "Lucknow",
        # "Remote India",
    ],

    "CHECK_EVERY_MINUTES": int(os.getenv("CHECK_EVERY_MINUTES", "30")),
    "MAX_AGE_HOURS": float(os.getenv("MAX_AGE_HOURS", "2")),
    "DATABASE_FILE": str(BASE_DIR / os.getenv("DATABASE_FILE", "job_alerts.db")),
    "LEGACY_SEEN_JOBS_FILE": str(BASE_DIR / "seen_jobs.json"),
    "CHECK_JD": env_bool("CHECK_JD", True),

    # Strict mode: reject unless title or JD explicitly says
    # intern/fresher/entry-level/graduate trainee/0-1 years.
    "STRICT_FRESHER_ONLY": env_bool("STRICT_FRESHER_ONLY", True),

    # Allow up to 1 year experience (internships + freshers + 1 year exp)
    "MAX_FRESHER_RANGE_END": int(os.getenv("MAX_FRESHER_RANGE_END", "1")),

    "SIMILARITY_THRESHOLD": float(os.getenv("SIMILARITY_THRESHOLD", "0.91")),
    "DEDUPE_LOOKBACK_DAYS": int(os.getenv("DEDUPE_LOOKBACK_DAYS", "90")),
    # Longer delays reduce the chance of LinkedIn 429 rate-limiting.
    "REQUEST_DELAY_SECONDS": float(os.getenv("REQUEST_DELAY_SECONDS", "3.5")),
    "JD_DELAY_SECONDS": float(os.getenv("JD_DELAY_SECONDS", "1.2")),
    "RATE_LIMIT_BACKOFF_SECONDS": float(os.getenv("RATE_LIMIT_BACKOFF_SECONDS", "90")),
    "USE_HF_CLASSIFIER": env_bool("USE_HF_CLASSIFIER", False),
    "HF_TOKEN": (
        os.getenv("HUGGINGFACE_TOKEN", "").strip()
        or os.getenv("HF_TOKEN", "").strip()
    ),
    "HF_MODEL": os.getenv("HF_MODEL", "facebook/bart-large-mnli").strip(),
    "HF_MIN_SCORE": float(os.getenv("HF_MIN_SCORE", "0.60")),
    "HF_FAIL_CLOSED": env_bool("HF_FAIL_CLOSED", False),

    "BLOCKED_COMPANIES": [
        "scutit",
        "internmo",
        "wake up whistle",
        "mnc job wala",
        "zenithbyte",
        "neural charter",
        "consultancy",
        "staffing",
        "placement",
        "recruitment agency",
        "dexter's tech",
        "medinex workforce",
        "nexal iit",
        "rytloop",
        "webboost solution it services",
    ],
}


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
log = logging.getLogger("job_alert")
log.setLevel(logging.INFO)
log.handlers.clear()

_file_handler = logging.FileHandler(BASE_DIR / "job_alert.log", encoding="utf-8")
_file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
log.addHandler(_file_handler)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter(LOG_FORMAT))
log.addHandler(_console_handler)
log.propagate = False


# -----------------------------------------------------------------------------
# Filter rules
# -----------------------------------------------------------------------------

REQUIRED_TITLE_KEYWORDS = [
    "machine learning",
    "ml engineer",
    "ml intern",
    "artificial intelligence",
    "ai engineer",
    "ai intern",
    "ai/ml",
    "ml/ai",
    "ai ml",
    "deep learning",
    "dl engineer",
    "nlp",
    "natural language processing",
    "computer vision",
    "cv engineer",
    "generative ai",
    "gen ai",
    "genai",
    "llm",
    "data scientist",
    "data science intern",
    "data analyst",
    "ml researcher",
    "ai researcher",
    "ml developer",
    "ai developer",
    "prompt engineer",
    "rag engineer",
]

BLOCKED_TITLE_KEYWORDS = [
    "senior",
    "sr.",
    "sr ",
    "lead",
    "principal",
    "manager",
    "director",
    "head of",
    "architect",
    "staff",
    "social media",
    "content writer",
    "graphic design",
    "video editing",
    "marketing",
    "sales",
    "business development",
    "entrepreneur",
    "excel",
    "power bi",
    "accounting",
    "customer support",
    "human resource",
    "web developer",
    "frontend",
    "backend",
    "full stack",
    "devops",
    "sre",
    "network engineer",
    "system admin",
    "qa",
    "test engineer",
    "manual testing",
    "3+ years",
    "4+ years",
    "5+ years",
    "3 years experience",
]

INDIA_KEYWORDS = [
    "india",
    "bengaluru",
    "bangalore",
    "hyderabad",
    "delhi",
    "new delhi",
    "noida",
    "greater noida",
    "gurugram",
    "chennai",
    "kolkata",
    "ahmedabad",
    "jaipur",
    "chandigarh",
    "bhopal",
    "lucknow",
    "remote",
]

AI_ML_JD_KEYWORDS = [
    "machine learning",
    "deep learning",
    "neural network",
    "tensorflow",
    "pytorch",
    "keras",
    "scikit",
    "nlp",
    "computer vision",
    "artificial intelligence",
    "data science",
    "model training",
    "transformers",
    "hugging face",
    "langchain",
    "generative ai",
    "gen ai",
    "genai",
    "llm",
    "large language model",
]

# Strong fresher/intern signals looked for in the JD body.
FRESHER_SIGNAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("intern/internship", re.compile(
        r"\b(?:this|an?|the)\s+internship\b|"
        r"\bintern(?:ship)?\s+(?:role|position|opening|opportunity|program|vacancy)\b|"
        r"\b(?:hiring|seeking|looking\s+for)\b[^.\n]{0,40}\b(?:ai|ml|machine\s+learning|data\s+science|generative\s+ai|llm|nlp|computer\s+vision)?\s*intern\b|"
        r"\b(?:ai|ml|machine\s+learning|data\s+science|deep\s+learning|nlp|computer\s+vision|generative\s+ai|llm)\s+intern\b|"
        r"\b(?:employment|job)\s+type\s*:?\s*internship\b",
        re.I,
    )),
    ("fresher", re.compile(r"\bfreshers?\b|\bfresh\s+graduates?\b", re.I)),
    ("recent graduate", re.compile(r"\brecent\s+graduates?\b", re.I)),
    ("entry level", re.compile(r"\bentry[\s-]+level\b", re.I)),
    ("graduate trainee", re.compile(r"\bgraduate\s+trainee\b", re.I)),
    ("campus hiring", re.compile(r"\bcampus\s+(?:hire|hiring|recruitment)\b", re.I)),
    ("apprentice", re.compile(r"\bapprentice(?:ship)?\b", re.I)),
    ("no experience", re.compile(
        r"\b(?:no|zero)\s+(?:prior\s+|work\s+)?experience\b|"
        r"\bexperience\s+(?:is\s+)?not\s+required\b",
        re.I,
    )),
    # 0-1 year / up to 1 year / 1 year experience (accept these)
    ("0-1 years", re.compile(
        r"\b0\s*(?:-|–|—|to)\s*1\s*(?:years?|yrs?)\b|"
        r"\b(?:experience|exp)\s*:?\s*0\s*(?:-|–|—|to)\s*1\b|"
        r"\bupto?\s*1\s*(?:year|yr)\b|"
        r"\bup\s+to\s+1\s*(?:year|yr)\b",
        re.I,
    )),
    ("final-year students", re.compile(r"\bfinal[\s-]+year\s+students?\b", re.I)),
]

# Title-only fresher signals (lighter; just the word itself).
TITLE_FRESHER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("intern/internship", re.compile(r"\bintern(?:ship)?\b", re.I)),
    ("fresher", re.compile(r"\bfreshers?\b", re.I)),
    ("entry level", re.compile(r"\bentry[\s-]+level\b", re.I)),
    ("graduate trainee", re.compile(r"\bgraduate\s+trainee\b", re.I)),
    ("apprentice", re.compile(r"\bapprentice(?:ship)?\b", re.I)),
]

SPACE_RE = re.compile(r"\s+")
NON_WORD_RE = re.compile(r"[^a-z0-9]+")


def now_ist() -> datetime:
    return datetime.now(IST)


def keyword_in_text(keyword: str, text: str) -> bool:
    """Boundary-aware match, including keywords such as AI/ML and NLP."""
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])", text, re.I))


def has_any_keyword(text: str, keywords: list[str]) -> bool:
    return any(keyword_in_text(keyword, text) for keyword in keywords)


def normalize_spaces(value: str) -> str:
    return SPACE_RE.sub(" ", value or "").strip()


def normalize_company(value: str) -> str:
    value = NON_WORD_RE.sub(" ", (value or "").lower())
    value = re.sub(
        r"\b(?:private|pvt|limited|ltd|llp|incorporated|inc|technologies|technology)\b",
        " ",
        value,
    )
    return normalize_spaces(value)


def normalize_title(value: str) -> str:
    value = (value or "").lower()
    replacements = {
        "artificial intelligence": "ai",
        "machine learning": "ml",
        "data sciences": "data science",
        "internship": "intern",
        "fresher": "entry",
        "entry level": "entry",
        "entry-level": "entry",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    value = re.sub(r"\b(?:remote|hybrid|onsite|on-site|urgent hiring|immediate joining)\b", " ", value)
    return normalize_spaces(NON_WORD_RE.sub(" ", value))


def normalize_location(value: str) -> str:
    return normalize_spaces(NON_WORD_RE.sub(" ", (value or "").lower()))


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    # Strip tracking query params/fragments that make the same job look new.
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def extract_linkedin_id(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    match = re.search(r"(?:/view/[^/]*-|/view/)(\d+)$", path)
    return match.group(1) if match else ""


def fresher_signals(text: str, title_only: bool = False) -> list[str]:
    patterns = TITLE_FRESHER_PATTERNS if title_only else FRESHER_SIGNAL_PATTERNS
    return [name for name, pattern in patterns if pattern.search(text or "")]


def find_experience_rejection(text: str) -> Optional[str]:
    """Return a reason when text clearly requires experienced candidates.

    ALLOWED: internships, freshers, "0-1 year", "up to 1 year", "1 year".
    REJECTED: "1-2 years", "2+ years", "minimum 2 years", "3 years experience",
              "1+ years" (i.e. MORE than 1 year).
    """
    if not text:
        return None

    value = text.lower().replace("yrs", "years").replace("yr", "year")

    # Normalize number words so "at least two years" is caught.
    number_words = {
        "one": "1",
        "two": "2",
        "three": "3",
        "four": "4",
        "five": "5",
        "six": "6",
        "seven": "7",
        "eight": "8",
        "nine": "9",
        "ten": "10",
    }
    for word, number in number_words.items():
        value = re.sub(rf"\b{word}\b", number, value)

    max_end = CONFIG["MAX_FRESHER_RANGE_END"]  # 1

    # Range like "1-2 years", "2-3 years", "2-5 years" -> reject.
    # "0-1 years" is OK; "0-2 years" is rejected because high > 1.
    range_pattern = re.compile(
        r"\b(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*(?:years?|yoe)\b",
        re.I,
    )
    for match in range_pattern.finditer(value):
        low, high = int(match.group(1)), int(match.group(2))
        if high > max_end or low > max_end:
            return f"experience range {low}-{high} years"

    # "N+ years" - allow "1+", reject "2+", "3+", etc.
    # Note: "1+ years" is borderline (could mean >=1) so we still allow it to
    # avoid false positives; if you want to be stricter change `>= 2` to `>= 1`.
    plus_pattern = re.compile(r"\b(\d{1,2})\s*\+\s*(?:years?|yoe)\b", re.I)
    for match in plus_pattern.finditer(value):
        years = int(match.group(1))
        if years > max_end:
            return f"requires {years}+ years"

    # "minimum N years", "at least N years", "more than N years", "over N years"
    minimum_pattern = re.compile(
        r"\b(?:minimum(?:\s+of)?|at\s+least|more\s+than|over)\s*"
        r"(?:relevant\s+|professional\s+|work\s+)?(?:experience\s*(?:of|:)?\s*)?"
        r"(\d{1,2})\s*(?:years?|yoe)\b",
        re.I,
    )
    for match in minimum_pattern.finditer(value):
        years = int(match.group(1))
        qualifier = match.group(0)
        if years > max_end or qualifier.startswith(("more", "over")) and years >= max_end:
            return qualifier

    # "must have N years", "requires N years", "N years of experience", etc.
    exact_requirement_patterns = [
        re.compile(
            r"\b(?:requires?|required|must\s+have|need(?:ed|s)?|with)\b"
            r"[^.\n;]{0,45}?\b(\d{1,2})\s*(?:years?|yoe)\b",
            re.I,
        ),
        re.compile(
            r"\b(\d{1,2})\s*years?\s+(?:of\s+)?(?:relevant\s+|professional\s+|work\s+)?"
            r"experience\b",
            re.I,
        ),
        re.compile(r"\bexperience\s*:?\s*(\d{1,2})\s*(?:years?|yoe)\b", re.I),
        re.compile(r"\b(\d{1,2})\s*yoe\b", re.I),
    ]
    for pattern in exact_requirement_patterns:
        for match in pattern.finditer(value):
            years = int(match.group(1))
            if years > max_end:
                return f"requires {years} years of experience"

    return None


def is_blocked_company(company: str) -> bool:
    company_lower = (company or "").lower()
    return any(blocked.lower() in company_lower for blocked in CONFIG["BLOCKED_COMPANIES"])


def is_valid_title(title: str) -> tuple[bool, str]:
    title = normalize_spaces(title)
    if not title:
        return False, "empty title"

    if has_any_keyword(title, BLOCKED_TITLE_KEYWORDS):
        return False, "blocked/experienced/irrelevant title"

    exp_reason = find_experience_rejection(title)
    if exp_reason:
        return False, exp_reason

    if not has_any_keyword(title, REQUIRED_TITLE_KEYWORDS):
        return False, "title has no target AI/ML role"

    return True, "target AI/ML title"


def is_india_location(location: str) -> bool:
    return has_any_keyword(location or "", INDIA_KEYWORDS)


def check_jd(title: str, jd_text: str) -> tuple[bool, str]:
    """Accept AI/ML roles targeted at interns/freshers/0-1 year candidates.

    We already ask LinkedIn for f_E=1,2 (Internship + Entry level). We reject
    the listing if:
      * the title screams experienced (handled before this function),
      * the JD explicitly asks for 2+ years, OR
      * the JD has zero AI/ML signals AND the title has no fresher/intern tag
        (misclassified listing).
    Otherwise we accept — we do NOT require a literal "fresher/intern/entry"
    keyword in the JD body because many legitimate entry-level JDs simply list
    responsibilities/skills without repeating "fresher".
    """
    title_signal_list = fresher_signals(title, title_only=True)

    title_exp = find_experience_rejection(title)
    if title_exp:
        return False, f"Title: {title_exp}"

    if not jd_text:
        if title_signal_list:
            return True, f"Title fresher signal: {title_signal_list[0]} | JD unavailable"
        # No JD and no title signal = can't verify → reject (safer).
        return False, "JD unavailable and title has no explicit fresher/intern signal"

    jd_exp = find_experience_rejection(jd_text)
    if jd_exp:
        return False, f"JD experienced role: {jd_exp}"

    ml_matches = [kw for kw in AI_ML_JD_KEYWORDS if keyword_in_text(kw, jd_text)]
    if not ml_matches and not title_signal_list:
        return False, "JD has no AI/ML context"

    jd_signal_list = fresher_signals(jd_text)
    all_signals = list(dict.fromkeys(title_signal_list + jd_signal_list))

    ml_summary = ", ".join(ml_matches[:2]) if ml_matches else "title"

    if all_signals:
        return True, f"AI/ML: {ml_summary} | Fresher: {', '.join(all_signals[:2])}"

    # No explicit fresher keyword in JD but:
    #   - JD has AI/ML keywords
    #   - JD does NOT require 2+ years (we already checked)
    #   - LinkedIn already filtered this as Internship/Entry-level (f_E=1,2)
    # → Treat as entry-level match.
    return True, f"AI/ML: {ml_summary} | Entry-level (no 2+ yr requirement found)"


# -----------------------------------------------------------------------------
# Data model and deduplication
# -----------------------------------------------------------------------------

@dataclass
class Job:
    title: str
    company: str
    location: str
    url: str
    posted_time: str
    jd_info: str = ""
    role_type: str = field(init=False)
    linkedin_id: str = field(init=False)
    job_key: str = field(init=False)
    fingerprint: str = field(init=False)
    title_norm: str = field(init=False)
    company_norm: str = field(init=False)
    location_norm: str = field(init=False)
    legacy_url_md5: str = field(init=False)

    def __post_init__(self) -> None:
        self.title = normalize_spaces(self.title)
        self.company = normalize_spaces(self.company)
        self.location = normalize_spaces(self.location)
        self.url = canonicalize_url(self.url)
        self.linkedin_id = extract_linkedin_id(self.url)
        self.title_norm = normalize_title(self.title)
        self.company_norm = normalize_company(self.company)
        self.location_norm = normalize_location(self.location)
        self.role_type = self._detect_role_type()

        stable_source = f"linkedin:{self.linkedin_id}" if self.linkedin_id else self.url.lower()
        self.job_key = hashlib.sha256(stable_source.encode("utf-8")).hexdigest()
        self.fingerprint = hashlib.sha256(
            f"{self.company_norm}|{self.title_norm}|{self.location_norm}".encode("utf-8")
        ).hexdigest()
        self.legacy_url_md5 = hashlib.md5(self.url.encode("utf-8")).hexdigest()

    def _detect_role_type(self) -> str:
        title = self.title.lower()
        if has_any_keyword(title, ["generative ai", "gen ai", "genai", "llm"]):
            return "GenAI"
        if has_any_keyword(title, ["computer vision", "cv engineer"]):
            return "CV"
        if has_any_keyword(title, ["natural language processing", "nlp"]):
            return "NLP"
        if has_any_keyword(title, ["deep learning", "dl engineer"]):
            return "DL"
        if has_any_keyword(title, ["data scientist", "data science", "data analyst"]):
            return "DS"
        if has_any_keyword(title, ["machine learning", "ml engineer", "ml intern", "ai/ml", "ml/ai", "mlops"]):
            return "ML"
        return "AI"


def titles_are_similar(first: Job, second: Job) -> bool:
    if first.company_norm != second.company_norm or first.role_type != second.role_type:
        return False
    ratio = SequenceMatcher(None, first.title_norm, second.title_norm).ratio()
    return ratio >= CONFIG["SIMILARITY_THRESHOLD"]


class CycleDeduper:
    """Prevents exact and fuzzy duplicates within one check cycle."""

    def __init__(self) -> None:
        self.job_keys: set[str] = set()
        self.linkedin_ids: set[str] = set()
        self.fingerprints: set[str] = set()
        self.accepted_jobs: list[Job] = []

    def is_exact_duplicate(self, job: Job) -> bool:
        return (
            job.job_key in self.job_keys
            or bool(job.linkedin_id and job.linkedin_id in self.linkedin_ids)
            or job.fingerprint in self.fingerprints
        )

    def remember_exact(self, job: Job) -> None:
        self.job_keys.add(job.job_key)
        if job.linkedin_id:
            self.linkedin_ids.add(job.linkedin_id)
        self.fingerprints.add(job.fingerprint)

    def is_similar_to_accepted(self, job: Job) -> bool:
        return any(titles_are_similar(job, existing) for existing in self.accepted_jobs)

    def remember_accepted(self, job: Job) -> None:
        self.accepted_jobs.append(job)


class JobStore:
    """Persistent deduplication DB (does not reset daily)."""

    def __init__(self, filepath: str) -> None:
        self.filepath = filepath
        self.connection = sqlite3.connect(filepath, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self._create_schema()
        self.legacy_seen = self._load_legacy_seen_ids()
        self._delete_old_rows()
        log.info("Dedup DB ready: %s", filepath)

    def _create_schema(self) -> None:
        with self.connection:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS notified_jobs (
                    job_key TEXT PRIMARY KEY,
                    linkedin_id TEXT,
                    fingerprint TEXT NOT NULL,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT NOT NULL,
                    title_norm TEXT NOT NULL,
                    company_norm TEXT NOT NULL,
                    location_norm TEXT NOT NULL,
                    role_type TEXT NOT NULL,
                    url TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'sent',
                    notified_at TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_linkedin_id ON notified_jobs(linkedin_id)"
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON notified_jobs(fingerprint)"
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_company_role ON notified_jobs(company_norm, role_type)"
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS hf_cache (
                    text_hash TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    score REAL NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def _load_legacy_seen_ids(self) -> set[str]:
        path = Path(CONFIG["LEGACY_SEEN_JOBS_FILE"])
        if not path.exists():
            return set()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ids = set(data.get("job_ids", []))
            log.info("Loaded %d IDs from legacy seen_jobs.json", len(ids))
            return ids
        except (OSError, ValueError, TypeError) as exc:
            log.warning("Could not read legacy seen_jobs.json: %s", exc)
            return set()

    def _delete_old_rows(self) -> None:
        cutoff = (now_ist() - timedelta(days=CONFIG["DEDUPE_LOOKBACK_DAYS"])).isoformat()
        with self.connection:
            self.connection.execute("DELETE FROM notified_jobs WHERE notified_at < ?", (cutoff,))
            self.connection.execute(
                "DELETE FROM hf_cache WHERE created_at < ?",
                ((now_ist() - timedelta(days=30)).isoformat(),),
            )

    def duplicate_reason(self, job: Job) -> Optional[str]:
        if job.legacy_url_md5 in self.legacy_seen:
            return "found in legacy seen_jobs.json"

        row = self.connection.execute(
            """
            SELECT job_key, linkedin_id, fingerprint
            FROM notified_jobs
            WHERE job_key = ?
               OR (? <> '' AND linkedin_id = ?)
               OR fingerprint = ?
            LIMIT 1
            """,
            (job.job_key, job.linkedin_id, job.linkedin_id, job.fingerprint),
        ).fetchone()
        if row:
            return "exact job already reserved/sent"

        cutoff = (now_ist() - timedelta(days=CONFIG["DEDUPE_LOOKBACK_DAYS"])).isoformat()
        rows = self.connection.execute(
            """
            SELECT title_norm
            FROM notified_jobs
            WHERE company_norm = ? AND role_type = ? AND notified_at >= ?
            """,
            (job.company_norm, job.role_type, cutoff),
        ).fetchall()
        for row in rows:
            similarity = SequenceMatcher(None, job.title_norm, row["title_norm"]).ratio()
            if similarity >= CONFIG["SIMILARITY_THRESHOLD"]:
                return f"similar company/title already sent ({similarity:.0%})"
        return None

    def reserve(self, jobs: list[Job]) -> None:
        timestamp = now_ist().isoformat()
        with self.connection:
            for job in jobs:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO notified_jobs (
                        job_key, linkedin_id, fingerprint, title, company, location,
                        title_norm, company_norm, location_norm, role_type, url,
                        status, notified_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        job.job_key,
                        job.linkedin_id,
                        job.fingerprint,
                        job.title,
                        job.company,
                        job.location,
                        job.title_norm,
                        job.company_norm,
                        job.location_norm,
                        job.role_type,
                        job.url,
                        timestamp,
                    ),
                )

    def mark_sent(self, jobs: list[Job]) -> None:
        with self.connection:
            self.connection.executemany(
                "UPDATE notified_jobs SET status = 'sent', notified_at = ? WHERE job_key = ?",
                [(now_ist().isoformat(), job.job_key) for job in jobs],
            )

    def release(self, jobs: list[Job]) -> None:
        with self.connection:
            self.connection.executemany(
                "DELETE FROM notified_jobs WHERE job_key = ? AND status = 'pending'",
                [(job.job_key,) for job in jobs],
            )

    def get_hf_cache(self, text_hash: str) -> Optional[tuple[str, float]]:
        row = self.connection.execute(
            "SELECT label, score FROM hf_cache WHERE text_hash = ?", (text_hash,)
        ).fetchone()
        if not row:
            return None
        return str(row["label"]), float(row["score"])

    def put_hf_cache(self, text_hash: str, label: str, score: float) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO hf_cache(text_hash, label, score, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (text_hash, label, score, now_ist().isoformat()),
            )


# -----------------------------------------------------------------------------
# Optional Hugging Face verifier
# -----------------------------------------------------------------------------

class HuggingFaceVerifier:
    LABEL_FRESHER = "fresher or internship role for a candidate with zero to one years experience"
    LABEL_EXPERIENCED = "experienced role requiring two or more years of prior work experience"
    LABEL_UNCLEAR = "unclear job experience level"

    def __init__(self, store: JobStore) -> None:
        self.store = store
        self.token = CONFIG["HF_TOKEN"]
        self.enabled = bool(CONFIG["USE_HF_CLASSIFIER"] and self.token)
        self.url = (
            "https://router.huggingface.co/hf-inference/models/"
            + CONFIG["HF_MODEL"]
        )
        self.session = requests.Session()
        if CONFIG["USE_HF_CLASSIFIER"] and not self.token:
            log.warning("USE_HF_CLASSIFIER=true but HUGGINGFACE_TOKEN/HF_TOKEN is missing; using local rules")
        elif self.enabled:
            log.info("Hugging Face verification enabled: %s", CONFIG["HF_MODEL"])

    def verify(self, title: str, jd_text: str) -> tuple[bool, str]:
        if not self.enabled:
            return True, "HF disabled"

        compact_jd = normalize_spaces(jd_text)[:3500]
        classifier_text = (
            f"Job title: {title}. Job description and requirements: {compact_jd}"
        )
        text_hash = hashlib.sha256(classifier_text.encode("utf-8")).hexdigest()
        cached = self.store.get_hf_cache(text_hash)
        if cached:
            label, score = cached
        else:
            result = self._request(classifier_text)
            if result is None:
                if CONFIG["HF_FAIL_CLOSED"]:
                    return False, "Hugging Face unavailable (fail-closed)"
                return True, "Hugging Face unavailable; strict local rules used"
            label, score = result
            self.store.put_hf_cache(text_hash, label, score)

        min_score = CONFIG["HF_MIN_SCORE"]
        if label == self.LABEL_EXPERIENCED and score >= min_score:
            return False, f"HF classified experienced ({score:.0%})"
        if label == self.LABEL_UNCLEAR and score >= min_score and CONFIG["HF_FAIL_CLOSED"]:
            return False, f"HF experience level unclear ({score:.0%})"
        return True, f"HF: {label} ({score:.0%})"

    def _request(self, text: str) -> Optional[tuple[str, float]]:
        payload = {
            "inputs": text,
            "parameters": {
                "candidate_labels": [
                    self.LABEL_FRESHER,
                    self.LABEL_EXPERIENCED,
                    self.LABEL_UNCLEAR,
                ],
                "multi_label": False,
            },
            "options": {"wait_for_model": True},
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        try:
            response = self.session.post(self.url, headers=headers, json=payload, timeout=45)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list) and data and isinstance(data[0], dict):
                data = data[0]
            labels = data.get("labels", []) if isinstance(data, dict) else []
            scores = data.get("scores", []) if isinstance(data, dict) else []
            if not labels or not scores:
                raise ValueError(f"unexpected response: {str(data)[:200]}")
            return str(labels[0]), float(scores[0])
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            log.warning("Hugging Face verification failed: %s", exc)
            return None


# -----------------------------------------------------------------------------
# LinkedIn public-listing scraper
# -----------------------------------------------------------------------------

class LinkedInScraper:
    BASE_URL = "https://www.linkedin.com/jobs/search/"
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)

    def _params(self, query: str, location: str) -> dict[str, str | int]:
        seconds = max(60, int(CONFIG["MAX_AGE_HOURS"] * 3600))
        return {
            "keywords": query,
            "location": location,
            "f_TPR": f"r{seconds}",
            # f_E: LinkedIn experience filter
            #   1 = Internship
            #   2 = Entry level (0-1 years approx)
            # We deliberately do NOT add 3 (Associate = 2+ yrs) — that would let
            # experienced roles leak in.
            "f_E": "1,2",
            # f_JT: F=Full-time, I=Internship, P=Part-time, C=Contract
            "f_JT": "F,I,P",
            "sortBy": "DD",
            "position": 1,
            "pageNum": 0,
        }

    def fetch_jd(self, job_url: str) -> str:
        try:
            response = self.session.get(job_url, timeout=15)
            if response.status_code == 429:
                backoff = CONFIG["RATE_LIMIT_BACKOFF_SECONDS"]
                log.warning("JD fetch got 429 — backing off %.0fs", backoff)
                time.sleep(backoff)
                return ""
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            jd_element = (
                soup.find("div", class_="description__text")
                or soup.find("div", class_="show-more-less-html__markup")
                or soup.find("section", class_="description")
            )
            if jd_element:
                return normalize_spaces(jd_element.get_text(separator=" ", strip=True))[:6000]
        except requests.RequestException as exc:
            log.debug("JD fetch failed for %s: %s", job_url, exc)
        return ""

    def fetch_jobs(self, query: str, location: str) -> list[Job]:
        log.info("Fetching: %s | %s", query, location)
        try:
            response = self.session.get(
                self.BASE_URL,
                params=self._params(query, location),
                timeout=20,
            )
            if response.status_code == 429:
                backoff = CONFIG["RATE_LIMIT_BACKOFF_SECONDS"]
                log.warning("LinkedIn 429 rate-limit hit — sleeping %.0fs then skipping this query/location", backoff)
                time.sleep(backoff)
                return []
            response.raise_for_status()
        except requests.RequestException as exc:
            log.warning("LinkedIn request failed: %s", exc)
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        cards = soup.find_all("div", class_="base-card")
        log.info("  Cards found: %d", len(cards))

        jobs: list[Job] = []
        rejects = {"title": 0, "company": 0, "location": 0, "malformed": 0}

        for card in cards:
            try:
                title_element = card.find("h3", class_="base-search-card__title")
                company_element = card.find("h4", class_="base-search-card__subtitle")
                location_element = card.find("span", class_="job-search-card__location")
                time_element = card.find("time")
                link_element = card.find("a", class_="base-card__full-link")

                if not title_element or not company_element or not link_element:
                    rejects["malformed"] += 1
                    continue

                title = title_element.get_text(strip=True)
                company = company_element.get_text(strip=True)
                job_location = (
                    location_element.get_text(strip=True) if location_element else location
                )
                job_url = link_element.get("href", "")
                if not job_url:
                    rejects["malformed"] += 1
                    continue

                if is_blocked_company(company):
                    rejects["company"] += 1
                    continue

                valid_title, _reason = is_valid_title(title)
                if not valid_title:
                    rejects["title"] += 1
                    continue

                if not is_india_location(job_location):
                    rejects["location"] += 1
                    continue

                jobs.append(
                    Job(
                        title=title,
                        company=company,
                        location=job_location,
                        url=job_url,
                        posted_time=(
                            time_element.get("datetime", "Unknown")
                            if time_element
                            else "Unknown"
                        ),
                    )
                )
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                rejects["malformed"] += 1
                log.debug("LinkedIn card parse error: %s", exc)

        log.info(
            "  Listing stats: %d cards | %d title | %d company | %d location | "
            "%d malformed | %d candidates",
            len(cards),
            rejects["title"],
            rejects["company"],
            rejects["location"],
            rejects["malformed"],
            len(jobs),
        )
        return jobs


# -----------------------------------------------------------------------------
# Email
# -----------------------------------------------------------------------------

class EmailNotifier:
    def __init__(self, sender: str, password: str, recipient: str) -> None:
        self.sender = sender
        self.password = password
        self.recipients = [item.strip() for item in recipient.split(",") if item.strip()]

    def is_configured(self) -> bool:
        return bool(self.sender and self.password and self.recipients)

    def send(self, jobs: list[Job]) -> bool:
        if not jobs:
            return True
        if not self.is_configured():
            log.error("Email is not configured. Set EMAIL_USER, EMAIL_PASSWORD and EMAIL_TO in .env")
            return False

        timestamp = now_ist()
        subject = (
            f"🎯 {len(jobs)} AI/ML Intern & Entry-Level Job"
            f"{'s' if len(jobs) != 1 else ''} Found! | {timestamp.strftime('%I:%M %p')}"
        )

        rows: list[str] = []
        plain_lines: list[str] = []
        colors = {
            "ML": "#e74c3c",
            "DS": "#3498db",
            "DL": "#9b59b6",
            "NLP": "#e67e22",
            "CV": "#1abc9c",
            "GenAI": "#f39c12",
            "AI": "#2ecc71",
        }

        for job in jobs:
            title = html.escape(job.title)
            company = html.escape(job.company)
            location = html.escape(job.location)
            url = html.escape(job.url, quote=True)
            posted = html.escape(job.posted_time)
            jd_info = html.escape(job.jd_info)
            badge_color = colors.get(job.role_type, "#95a5a6")
            jd_note = (
                f"<br><small style='color:#17743a'>{jd_info}</small>" if jd_info else ""
            )
            rows.append(
                f"""
                <tr>
                  <td style="padding:12px;border-bottom:1px solid #eee">
                    <a href="{url}" style="color:#0077B5;font-weight:bold;text-decoration:none">{title}</a>
                    <br><span style="background:{badge_color};color:white;padding:3px 8px;border-radius:4px;font-size:11px;font-weight:bold">{job.role_type}</span>{jd_note}
                  </td>
                  <td style="padding:12px;border-bottom:1px solid #eee">{company}</td>
                  <td style="padding:12px;border-bottom:1px solid #eee">{location}</td>
                  <td style="padding:12px;border-bottom:1px solid #eee;color:#666;font-size:12px">{posted}</td>
                  <td style="padding:12px;border-bottom:1px solid #eee">
                    <a href="{url}" style="background:#0077B5;color:white;padding:8px 16px;border-radius:4px;text-decoration:none;font-size:13px;font-weight:bold">Apply</a>
                  </td>
                </tr>
                """
            )
            plain_lines.append(
                f"- {job.title} | {job.company} | {job.location}\n  {job.url}\n  {job.jd_info}"
            )

        html_body = f"""
        <html><body style="font-family:Arial,sans-serif;max-width:1000px;margin:auto;padding:20px">
          <div style="background:#0077B5;color:white;padding:25px;border-radius:10px 10px 0 0">
            <h2 style="margin:0">🎯 AI/ML Intern &amp; Entry-Level Job Alert</h2>
            <p style="margin:8px 0 0">{len(jobs)} strict intern / fresher / 0-1 year matches</p>
          </div>
          <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #ddd;border-collapse:collapse">
            <thead><tr style="background:#f8f9fa">
              <th style="padding:14px;text-align:left">Job Title</th>
              <th style="padding:14px;text-align:left">Company</th>
              <th style="padding:14px;text-align:left">Location</th>
              <th style="padding:14px;text-align:left">Posted</th>
              <th style="padding:14px;text-align:left">Action</th>
            </tr></thead>
            <tbody>{''.join(rows)}</tbody>
          </table>
          <div style="background:#e8f5e9;padding:15px;border:1px solid #ddd;border-radius:0 0 10px 10px;font-size:13px">
            ✅ {timestamp.strftime('%d %b %Y, %I:%M:%S %p')} IST | Persistent similarity dedupe active
          </div>
        </body></html>
        """

        plain_body = (
            f"AI/ML intern & entry-level job alert: {len(jobs)} match(es)\n\n"
            + "\n\n".join(plain_lines)
        )
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        message.attach(MIMEText(plain_body, "plain", "utf-8"))
        message.attach(MIMEText(html_body, "html", "utf-8"))

        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
                server.login(self.sender, self.password)
                server.sendmail(self.sender, self.recipients, message.as_string())
            log.info("📧 Email sent: %d jobs", len(jobs))
            return True
        except (OSError, smtplib.SMTPException) as exc:
            log.error("❌ Email error: %s", exc)
            return False


# -----------------------------------------------------------------------------
# Monitor
# -----------------------------------------------------------------------------

class JobMonitor:
    def __init__(self) -> None:
        self.scraper = LinkedInScraper()
        self.store = JobStore(CONFIG["DATABASE_FILE"])
        self.hf_verifier = HuggingFaceVerifier(self.store)
        self.notifier = EmailNotifier(
            CONFIG["EMAIL_SENDER"],
            CONFIG["EMAIL_PASSWORD"],
            CONFIG["EMAIL_TO"],
        )

    def check_once(self) -> None:
        log.info("\n%s", "=" * 72)
        log.info("CHECK: %s", now_ist().strftime("%d %b %Y %I:%M:%S %p IST"))

        cycle = CycleDeduper()
        new_jobs: list[Job] = []
        stats = {
            "candidates": 0,
            "cycle_exact": 0,
            "db_duplicate": 0,
            "jd_reject": 0,
            "hf_reject": 0,
            "cycle_similar": 0,
        }

        for query in CONFIG["SEARCH_QUERIES"]:
            for location in CONFIG["LOCATIONS"]:
                jobs = self.scraper.fetch_jobs(query, location)
                stats["candidates"] += len(jobs)

                for job in jobs:
                    # Exact duplicate check before JD fetch saves requests.
                    if cycle.is_exact_duplicate(job):
                        stats["cycle_exact"] += 1
                        continue
                    cycle.remember_exact(job)

                    db_reason = self.store.duplicate_reason(job)
                    if db_reason:
                        stats["db_duplicate"] += 1
                        log.info("  SKIP DB duplicate: %s | %s (%s)", job.title, job.company, db_reason)
                        continue

                    jd_text = self.scraper.fetch_jd(job.url) if CONFIG["CHECK_JD"] else ""
                    valid, jd_reason = check_jd(job.title, jd_text)
                    if not valid:
                        stats["jd_reject"] += 1
                        log.info("  REJECT: %s | %s — %s", job.title, job.company, jd_reason)
                        continue

                    hf_valid, hf_reason = self.hf_verifier.verify(job.title, jd_text)
                    if not hf_valid:
                        stats["hf_reject"] += 1
                        log.info("  HF REJECT: %s | %s — %s", job.title, job.company, hf_reason)
                        continue

                    if cycle.is_similar_to_accepted(job):
                        stats["cycle_similar"] += 1
                        log.info("  SKIP similar in this cycle: %s | %s", job.title, job.company)
                        continue

                    job.jd_info = jd_reason
                    if self.hf_verifier.enabled:
                        job.jd_info += f" | {hf_reason}"
                    new_jobs.append(job)
                    cycle.remember_accepted(job)
                    log.info("  ✅ NEW MATCH: %s | %s | %s", job.title, job.company, job.role_type)
                    # Jittered pause between JD fetches.
                    jd_delay = CONFIG["JD_DELAY_SECONDS"]
                    time.sleep(jd_delay + random.uniform(0, jd_delay * 0.6))

                # Jittered pause between LinkedIn search pages.
                req_delay = CONFIG["REQUEST_DELAY_SECONDS"]
                time.sleep(req_delay + random.uniform(0, req_delay * 0.6))

        log.info("\n📊 SUMMARY")
        log.info("   Listing candidates: %d", stats["candidates"])
        log.info("   Same-cycle exact duplicates: %d", stats["cycle_exact"])
        log.info("   DB exact/similar duplicates: %d", stats["db_duplicate"])
        log.info("   Strict fresher/JD rejects: %d", stats["jd_reject"])
        log.info("   Hugging Face rejects: %d", stats["hf_reject"])
        log.info("   Same-cycle similar duplicates: %d", stats["cycle_similar"])
        log.info("   New jobs to email: %d", len(new_jobs))

        if not new_jobs:
            log.info("No new intern/fresher/0-1yr jobs this round")
            return

        self.store.reserve(new_jobs)
        if self.notifier.send(new_jobs):
            self.store.mark_sent(new_jobs)
        else:
            self.store.release(new_jobs)
            log.warning("Email failed; DB reservation removed so the next cycle can retry")

    def run(self) -> None:
        log.info("🚀 STARTED: AI/ML intern & entry-level (0-1 yr) job alert")
        log.info("   Queries: %d", len(CONFIG["SEARCH_QUERIES"]))
        log.info("   Locations: %s", CONFIG["LOCATIONS"])
        log.info("   Interval: %d minutes", CONFIG["CHECK_EVERY_MINUTES"])
        log.info("   Time window: %s hours", CONFIG["MAX_AGE_HOURS"])
        log.info("   Persistent dedupe: %d days", CONFIG["DEDUPE_LOOKBACK_DAYS"])
        log.info("   Hugging Face: %s", "enabled" if self.hf_verifier.enabled else "disabled")

        self.check_once()
        schedule.every(CONFIG["CHECK_EVERY_MINUTES"]).minutes.do(self.check_once)
        while True:
            schedule.run_pending()
            time.sleep(30)


def validate_config() -> None:
    if CONFIG["CHECK_EVERY_MINUTES"] < 1:
        raise ValueError("CHECK_EVERY_MINUTES must be at least 1")
    if CONFIG["MAX_AGE_HOURS"] <= 0:
        raise ValueError("MAX_AGE_HOURS must be greater than 0")
    if not 0.0 <= CONFIG["SIMILARITY_THRESHOLD"] <= 1.0:
        raise ValueError("SIMILARITY_THRESHOLD must be between 0 and 1")


if __name__ == "__main__":
    validate_config()
    JobMonitor().run()
