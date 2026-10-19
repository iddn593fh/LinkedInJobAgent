"""Text helpers: content fingerprint for dedup, and YoE / salary parsing (hints for the LLM)."""
import hashlib
import re

# --- fingerprint -------------------------------------------------------------

def fingerprint(company, title, location):
    t = (title or "").lower()
    t = re.sub(r"\((.*?)\)", " ", t)                    # "(Remote)" tags
    t = re.sub(r"\b(i{1,3}|iv|v|vi|[1-5])\b", " ", t)   # level suffixes
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    c = re.sub(r"[^a-z0-9]", "", (company or "").lower())
    loc = re.sub(r"[^a-z0-9]", "", (location or "").lower()[:40])
    return hashlib.sha256(f"{c}|{t}|{loc}".encode()).hexdigest()


# --- years of experience -----------------------------------------------------

_N = r"(\d{1,2})"
_YRS = r"\s*(?:years?|yrs?)\b"
YOE_PATTERNS = [
    re.compile(_N + r"\s*(?:-|–|—|to)\s*\d{1,2}\s*\+?" + _YRS, re.I),   # 5-8 years
    re.compile(_N + r"\s*\+" + _YRS, re.I),                              # 3+ years
    re.compile(r"at\s+least\s+" + _N + r"\+?" + _YRS, re.I),            # at least 4 years
    re.compile(r"minimum\s+(?:of\s+)?" + _N + r"\+?" + _YRS, re.I),     # minimum of 2 years
]


def parse_yoe(text, max_chars=6000):
    """Smallest stated minimum years of experience, or None. Values > 25 are ignored."""
    if not text:
        return None
    text = text[:max_chars]
    found = []
    for pat in YOE_PATTERNS:
        for m in pat.finditer(text):
            n = int(m.group(1))
            if n <= 25:
                found.append(n)
    return min(found) if found else None


# --- salary ------------------------------------------------------------------

_AMT = r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d+)?\s?[kK]\b|\d{5,6}(?:\.\d{1,2})?)"
SALARY_RE = re.compile(
    r"(?:\$|USD\s*)\s?" + _AMT + r"(?:\s*(?:-|–|—|to)\s*(?:\$|USD\s*)?\s?" + _AMT + r")?", re.I)
SALARY_MIN, SALARY_MAX = 60_000, 900_000


def _amount(s):
    s = s.replace(",", "").strip()
    if s[-1:] in "kK":
        return int(float(s[:-1].strip()) * 1000)
    return int(float(s))


def parse_salary(text):
    """(min, max) of annual dollar figures in [$60k, $900k], or (None, None).

    Only figures prefixed with $ or USD count; anything outside the band (hourly rates,
    street numbers, phone numbers, equity totals) is ignored.
    """
    if not text:
        return None, None
    vals = []
    for m in SALARY_RE.finditer(text):
        for g in m.groups():
            if g:
                v = _amount(g)
                if SALARY_MIN <= v <= SALARY_MAX:
                    vals.append(v)
    return (min(vals), max(vals)) if vals else (None, None)
