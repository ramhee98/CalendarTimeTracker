"""Calendar feed health: what happened each time a calendar URL was fetched.

Every fetch (Sync buttons, the background refresh, checks on the Calendar
Health page) records one check per calendar in sync_health.json: whether
the URL answered, whether the answer was a calendar, how many events it had
and how long it took. calendar_status() turns a calendar's history into
OK / warning / failing, with tolerance for a single failed fetch.

No Streamlit here, so the background refresher can record checks too. URLs
are stored masked (they often contain a secret key) and keyed by their hash.
"""
import json
import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

from calendar_store import decode_ics_response, hash_url

HEALTH_FILE = "sync_health.json"
MAX_CHECKS = 200  # per calendar
FETCH_TIMEOUT = 30  # seconds

DEFAULT_THRESHOLDS = {
    "fail_threshold": 2,     # failed fetches in a row before a calendar is failing
    "drop_percent": 50,      # warn when the event count drops by this much
    "frozen_days": 45,       # warn when nothing in the feed changed for this long (0 = off)
    "slow_seconds": 10,      # warn when a fetch takes longer
    "stale_hours": 48,       # warn when the last check is older
}

_lock = threading.Lock()


class FeedError(Exception):
    """A feed that couldn't be fetched or isn't a calendar.

    `kind`: "timeout", "dns", "ssl", "connection", "http", "not_ics",
    "parse", "file" or "error".
    """

    def __init__(self, kind, message, http_status=None, ms=None):
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status
        self.ms = ms


ERROR_LABELS = {
    "timeout": "Timed out",
    "dns": "Server not found",
    "ssl": "SSL / certificate error",
    "connection": "Connection failed",
    "http": "HTTP error",
    "not_ics": "Not a calendar",
    "parse": "Couldn't be read",
    "file": "File not readable",
    "error": "Error",
}

HTTP_HINTS = {
    401: "access denied: the link may need a login",
    403: "access denied: the secret link may have been reset",
    404: "not found: the calendar or its secret link may have been deleted or reset",
    410: "gone: the calendar was removed",
    429: "too many requests: the server is rate limiting",
}


def mask_url(url):
    """URL without anything secret: scheme, host and the last path part
    shortened ("https://calendar.google.com/…/basic.ics")."""
    parsed = urlparse(str(url))
    if parsed.scheme == "file":
        return "file://…/" + os.path.basename(parsed.path)
    last = os.path.basename(parsed.path.rstrip("/"))
    if len(last) > 24:
        last = last[:6] + "…" + last[-6:]
    return f"{parsed.scheme}://{parsed.hostname or ''}/…/{last}" if last else f"{parsed.scheme}://{parsed.hostname}/…"


def fetch_feed(url, timeout=FETCH_TIMEOUT):
    """Text of a calendar URL (http(s) or file://) and {"ms", "http_status",
    "bytes"}. Raises FeedError with a classified reason."""
    started = time.monotonic()
    ms = lambda: round((time.monotonic() - started) * 1000)
    parsed = urlparse(url)
    if parsed.scheme == "file":
        try:
            with open(parsed.path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            raise FeedError("file", str(e), ms=ms())
        return text, {"ms": ms(), "http_status": None, "bytes": len(text.encode("utf-8"))}
    try:
        response = requests.get(url, timeout=timeout)
    except requests.exceptions.Timeout:
        raise FeedError("timeout", f"No answer within {timeout} s", ms=ms())
    except requests.exceptions.SSLError as e:
        raise FeedError("ssl", _short(e), ms=ms())
    except requests.exceptions.ConnectionError as e:
        kind = "dns" if _is_dns_error(e) else "connection"
        raise FeedError(kind, _short(e), ms=ms())
    except requests.exceptions.RequestException as e:
        raise FeedError("error", _short(e), ms=ms())
    if response.status_code != 200:
        hint = HTTP_HINTS.get(response.status_code, "server error" if response.status_code >= 500 else "")
        raise FeedError("http", f"HTTP {response.status_code}" + (f" ({hint})" if hint else ""),
                        http_status=response.status_code, ms=ms())
    text = decode_ics_response(response)
    return text, {"ms": ms(), "http_status": 200, "bytes": len(response.content)}


def check_ics_text(text, meta=None):
    """Raise FeedError("not_ics") unless `text` is an iCalendar feed."""
    if "BEGIN:VCALENDAR" in text:
        return
    head = text.lstrip()[:200].lower()
    if head.startswith("<!doctype html") or head.startswith("<html") or "<html" in head:
        message = "The URL returned a web page, not a calendar feed (a sharing link or a login page?)"
    elif not text.strip():
        message = "The URL returned an empty answer"
    else:
        message = "The URL didn't return an iCalendar feed (no BEGIN:VCALENDAR)"
    raise FeedError("not_ics", message, http_status=(meta or {}).get("http_status"), ms=(meta or {}).get("ms"))


def _is_dns_error(e):
    text = str(e).lower()
    return isinstance(getattr(e, "__context__", None), socket.gaierror) or any(
        s in text for s in ("name or service not known", "nodename nor servname", "failed to resolve",
                            "getaddrinfo failed", "name resolution"))


def _short(e, limit=200):
    text = str(e)
    return text if len(text) <= limit else text[:limit] + "…"


def feed_stats(expanded, errors=()):
    """Event statistics of a parsed feed: `expanded` as returned by
    calendar_store.expand_calendar_events (event, start, end, uid)."""
    events = [e for e, _, _, _ in expanded]
    newest_start = max((s for _, s, _, _ in expanded), default=None)
    modified = [e.last_modified for e in {id(e): e for e in events}.values() if getattr(e, "last_modified", None)]
    newest_change = max((m.datetime for m in modified), default=None)
    return {
        "events": len(expanded),
        "parse_errors": len(errors),
        "newest_event": _iso(newest_start),
        "last_changed": _iso(newest_change),
    }


def _iso(ts):
    if ts is None:
        return None
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


# --- Storage ---

def load_health(path=HEALTH_FILE):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and isinstance(data.get("calendars"), dict) else {"calendars": {}}
    except (FileNotFoundError, json.JSONDecodeError):
        return {"calendars": {}}


def record_check(url, name, ok, meta=None, stats=None, error=None, source="sync", path=HEALTH_FILE):
    """Append one check of `url` to the health file (thread-safe, atomic)."""
    meta, stats = meta or {}, stats or {}
    check = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": bool(ok),
        "source": source,
        "ms": meta.get("ms"),
        "http_status": meta.get("http_status"),
        "bytes": meta.get("bytes"),
    }
    if ok:
        check.update({k: stats.get(k) for k in ("events", "parse_errors", "newest_event", "last_changed")})
    else:
        check.update({"kind": getattr(error, "kind", "error"), "error": _short(error or "Unknown error", 300)})
    try:
        with _lock:
            data = load_health(path)
            entry = data["calendars"].setdefault(hash_url(url), {"checks": []})
            entry["name"] = name
            entry["url"] = mask_url(url)
            entry["checks"] = (entry.get("checks", []) + [check])[-MAX_CHECKS:]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, ensure_ascii=False)
            os.replace(tmp, path)
    except OSError as e:
        print(f"[CalendarTimeTracker] Could not write {path}: {e}")


def record_failure(url, name, error, source="sync"):
    """Record a failed fetch; `error` is a FeedError or any exception."""
    if not isinstance(error, FeedError):
        error = FeedError("parse" if isinstance(error, ValueError) else "error", _short(error))
    record_check(url, name, False, {"ms": error.ms, "http_status": error.http_status}, error=error, source=source)


def clear_health(path=HEALTH_FILE):
    with _lock:
        if os.path.exists(path):
            os.remove(path)


# --- Status ---

def _parse_time(text):
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def calendar_status(checks, now=None, thresholds=None):
    """Status of one calendar from its checks (oldest first).

    Returns {"level": "ok" | "warning" | "failing" | "unknown", "problems":
    [...], "failures_in_row", "failing_since", "last_check", "last_good",
    "uptime" (share of ok checks among the last 30), "last": last check}.
    """
    t = dict(DEFAULT_THRESHOLDS, **(thresholds or {}))
    now = now or datetime.now(timezone.utc)
    result = {"level": "unknown", "problems": [], "failures_in_row": 0, "failing_since": None,
              "last_check": None, "last_good": None, "uptime": None, "last": None}
    if not checks:
        result["problems"].append("Never checked: sync or check it once.")
        return result

    last = checks[-1]
    result["last"] = last
    result["last_check"] = _parse_time(last.get("time"))
    goods = [c for c in checks if c.get("ok")]
    result["last_good"] = _parse_time(goods[-1]["time"]) if goods else None
    recent = checks[-30:]
    result["uptime"] = sum(1 for c in recent if c.get("ok")) / len(recent)

    in_row = 0
    for c in reversed(checks):
        if c.get("ok"):
            break
        in_row += 1
    result["failures_in_row"] = in_row
    problems = result["problems"]

    if in_row:
        result["failing_since"] = _parse_time(checks[-in_row]["time"])
        if last.get("kind") == "http":
            reason = last.get("error") or "HTTP error"
        else:
            reason = f"{ERROR_LABELS.get(last.get('kind'), 'Error')}: {last.get('error', '')}".rstrip(": ")
        if in_row >= t["fail_threshold"]:
            result["level"] = "failing"
            problems.append(f"{reason} ({in_row} failed fetches in a row; the cached events are kept)")
        else:
            result["level"] = "warning"
            problems.append(f"Last fetch failed, will count as failing after {t['fail_threshold']} in a row: {reason}")
    else:
        result["level"] = "ok"
        previous = next((c for c in reversed(checks[:-1]) if c.get("ok")), None)
        events = last.get("events") or 0
        before = (previous or {}).get("events") or 0
        if events == 0:
            problems.append("The feed has no events" + (f" (it had {before} before)" if before else "")
                            + ": check that the link still points to the right calendar")
        elif before >= 10 and events < before * (1 - t["drop_percent"] / 100):
            problems.append(f"Event count dropped from {before} to {events}")
        if last.get("parse_errors"):
            problems.append(f"{last['parse_errors']} event(s) couldn't be read and were skipped")
        if t["frozen_days"]:
            changed = _parse_time(last.get("last_changed"))
            if changed and now - changed > timedelta(days=t["frozen_days"]):
                problems.append(f"Nothing in the feed changed for {(now - changed).days} days: "
                                "is the calendar still updated?")
        if (last.get("ms") or 0) > t["slow_seconds"] * 1000:
            problems.append(f"Slow: the fetch took {last['ms'] / 1000:.1f} s")
        if problems:
            result["level"] = "warning"

    if result["last_check"] and now - result["last_check"] > timedelta(hours=t["stale_hours"]):
        hours = (now - result["last_check"]).total_seconds() / 3600
        problems.append(f"Not checked for {hours / 24:.0f} days" if hours >= 48 else f"Not checked for {hours:.0f} h")
        if result["level"] == "ok":
            result["level"] = "warning"
    return result
