"""Extract job cards from LinkedIn job-alert email HTML.

Heuristic by necessity: LinkedIn changes templates without notice. Verify with
`linkedin_source.py debug` against real mail before trusting a scheduled run.
"""
import re
from urllib.parse import unquote

from bs4 import BeautifulSoup, NavigableString, Tag

JOB_ID_RE = re.compile(r"/jobs/view/(?:[^/?#&\s]*?-)?(\d{6,})")
SEE_ALL_RE = re.compile(r"\b(?:see|view)\s+all\s+([\d,]+)\+?\s+(?:new\s+)?jobs?\b", re.I)
# Current template: header "30+ new jobs in United States match your preferences.",
# footer just "See all jobs" with no number.
NEW_JOBS_RE = re.compile(r"\b([\d,]+)\+?\s+new\s+jobs?\b", re.I)
SEPARATOR_RE = re.compile(r"\s+[·•|]\s+")

GENERIC_ANCHOR = re.compile(
    r"^(apply|easy apply|apply now|view job|view jobs|see job|see jobs|view|more|learn more|"
    r"save|saved|view details)$", re.I)

# Lines inside a card that are neither company nor location.
NOISE_LINE = re.compile(
    r"""^(
        apply | easy\s+apply | apply\s+now | view\s+job | promoted | actively\s+recruiting |
        new | viewed | be\s+an\s+early\s+applicant | \d+\s+applicants? |
        \d+\s+(school\s+)?(alum(ni|nus)?|connections?)\b.* | .*\bconnections?\s+work\s+here.* |
        .*\bago$ | (posted|reposted)\b.* | your\s+profile\s+matches.* | top\s+applicant.* |
        .*\bmatch(es)?\s+your\s+preferences.* | ·|•|\|
    )$""", re.I | re.X)
SALARY_LINE = re.compile(r"\$\s?\d")

BLOCK_TAGS = {"p", "div", "td", "th", "tr", "table", "tbody", "li", "ul", "ol",
              "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "center"}


def job_id_from_href(href):
    """Find /jobs/view/<id> in a possibly tracking-wrapped, URL-encoded href."""
    h = href or ""
    for _ in range(4):
        m = JOB_ID_RE.search(h)
        if m:
            return m.group(1)
        decoded = unquote(h)
        if decoded == h:
            break
        h = decoded
    return None


def canonical_url(job_id):
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def block_lines(node):
    """Visible text of node split on block boundaries and <br>, whitespace-collapsed."""
    lines, cur = [], []

    def flush():
        s = re.sub(r"\s+", " ", "".join(cur)).strip()
        if s:
            lines.append(s)
        cur.clear()

    def walk(n):
        if isinstance(n, NavigableString):
            if n.__class__.__name__ in ("Comment", "Doctype", "Declaration", "ProcessingInstruction"):
                return
            cur.append(str(n))
            return
        if not isinstance(n, Tag) or n.name in ("style", "script", "head", "title"):
            return
        if n.name == "br":
            flush()
            return
        block = n.name in BLOCK_TAGS
        if block:
            flush()
        for c in n.children:
            walk(c)
        if block:
            flush()

    walk(node)
    flush()
    return lines


def _ids_in(node):
    return {i for a in node.find_all("a", href=True) if (i := job_id_from_href(a["href"]))}


def _card_container(anchor, job_id):
    """Nearest enclosing <td>/<table> holding more than the title, without a neighbouring card."""
    best = None
    node = anchor
    for _ in range(12):
        node = node.parent
        if node is None or node.name in ("body", "html", "[document]"):
            break
        if node.name not in ("td", "table"):
            continue
        if _ids_in(node) - {job_id}:
            break                      # grew into another job's card
        best = node
        if len(block_lines(node)) >= 2:
            return node
    return best


def _anchor_title(a):
    """First non-generic text line of an anchor (handles whole-card anchors)."""
    for line in block_lines(a):
        if not GENERIC_ANCHOR.match(line) and not NOISE_LINE.match(line):
            return line
    return ""


def _company_location(lines, title):
    """Pull company / location / salary text out of a card's lines."""
    try:
        start = lines.index(title) + 1
    except ValueError:
        start = 0
    rest = []
    salary_text = None
    for line in lines[start:]:
        if line == title or GENERIC_ANCHOR.match(line) or NOISE_LINE.match(line):
            continue
        if SALARY_LINE.search(line) and salary_text is None:
            salary_text = line
            continue
        rest.append(line)

    company = location = None
    for i, line in enumerate(rest):
        parts = [p.strip() for p in SEPARATOR_RE.split(line) if p.strip()]
        if len(parts) >= 2:                      # "Company · Location"
            company, location = parts[0], " · ".join(parts[1:])
            break
        if i == 0:
            company = line
        elif i == 1:
            location = line
            break
    return company, location, salary_text


def parse_alert_html(html):
    """Return {"cards": [...], "see_all": int|None}.

    Each card: job_id, title, company, location, salary_text, url.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    anchors_by_id = {}
    order = []
    for a in soup.find_all("a", href=True):
        jid = job_id_from_href(a["href"])
        if not jid:
            continue
        if jid not in anchors_by_id:
            anchors_by_id[jid] = []
            order.append(jid)
        anchors_by_id[jid].append(a)

    cards = []
    for jid in order:
        titled = [(a, _anchor_title(a)) for a in anchors_by_id[jid]]
        titled = [(a, t) for a, t in titled if t]
        if not titled:
            continue
        anchor, title = max(titled, key=lambda x: len(x[1]))   # longest anchor title per ID
        container = _card_container(anchor, jid)
        lines = block_lines(container) if container is not None else [title]
        company, location, salary_text = _company_location(lines, title)
        cards.append({
            "job_id": jid, "title": title, "company": company, "location": location,
            "salary_text": salary_text, "url": canonical_url(jid),
        })

    text = soup.get_text(" ", strip=True)
    counts = [int(m.group(1).replace(",", ""))
              for rx in (SEE_ALL_RE, NEW_JOBS_RE) for m in rx.finditer(text)]
    return {"cards": cards, "see_all": max(counts) if counts else None}
