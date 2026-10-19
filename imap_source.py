"""Config loading and IMAP access for LinkedIn alert emails (stdlib only)."""
import email
import imaplib
import os
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env(path=ENV_PATH):
    """Load KEY=VALUE lines into os.environ without overriding values already set."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _header(msg, name):
    raw = msg.get(name)
    return str(make_header(decode_header(raw))) if raw else ""


def html_part(msg):
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True) or b""
            return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return ""


def message_to_record(msg, uid=None):
    try:
        date = parsedate_to_datetime(msg.get("Date"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        date = None
    return {"uid": uid, "subject": _header(msg, "Subject"), "from": _header(msg, "From"),
            "date": date, "html": html_part(msg)}


def read_local_file(path):
    """A saved .eml, or a bare .html body, for offline debugging."""
    with open(path, "rb") as f:
        data = f.read()
    if path.lower().endswith((".html", ".htm")):
        return {"uid": None, "subject": os.path.basename(path), "from": "", "date": None,
                "html": data.decode("utf-8", errors="replace")}
    return message_to_record(email.message_from_bytes(data))


class Mailbox:
    def __init__(self, readonly=True):
        self.readonly = readonly
        self.conn = None

    def __enter__(self):
        host = os.environ.get("IMAP_HOST", "imap.gmail.com")
        user, password = os.environ.get("IMAP_USER"), os.environ.get("IMAP_PASSWORD")
        if not user or not password:
            raise SystemExit(f"IMAP_USER / IMAP_PASSWORD not set (expected in {ENV_PATH})")
        self.conn = imaplib.IMAP4_SSL(host)
        self.conn.login(user, password)
        folder = os.environ.get("IMAP_FOLDER", "INBOX")
        typ, _ = self.conn.select(f'"{folder}"', readonly=self.readonly)
        if typ != "OK":
            raise SystemExit(f"cannot select IMAP folder {folder!r}")
        return self

    def __exit__(self, *exc):
        try:
            self.conn.logout()
        except Exception:
            pass

    def alert_emails(self, hours):
        """Job-alert emails whose Date header is within the last `hours` hours.

        Only mail from IMAP_ALERT_FROM (default jobalerts-noreply@linkedin.com), so LinkedIn's
        security / verification mail and anything forwarded by hand are not read.
        IMAP SINCE is day-granular, so search a day wider and filter on the header.
        Uses BODY.PEEK so reading never sets \\Seen.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        since = (cutoff - timedelta(days=1)).strftime("%d-%b-%Y")
        sender = os.environ.get("IMAP_ALERT_FROM", "jobalerts-noreply@linkedin.com")
        typ, data = self.conn.uid("SEARCH", None, "SINCE", since, "FROM", f'"{sender}"')
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {data}")
        out = []
        for uid in data[0].split():
            typ, msg_data = self.conn.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            rec = message_to_record(email.message_from_bytes(msg_data[0][1]), uid.decode())
            if rec["date"] is None or rec["date"] >= cutoff:
                out.append(rec)
        out.sort(key=lambda r: r["date"] or cutoff)
        return out

    def mark_seen(self, uids):
        if self.readonly:
            raise RuntimeError("mailbox opened read-only")
        for uid in uids:
            self.conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
