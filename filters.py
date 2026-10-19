"""Deterministic helpers: title matching, YoE/salary parsing, fingerprints, company tiers."""
import hashlib
import os
import re

import yaml

TITLE_INCLUDE = re.compile(r"""(
    app(lication)?[\s\-]*sec(urity)? | product[\s\-]*sec(urity)? |
    offensive[\s\-]*sec(urity)? | penetration[\s\-]*test | pen[\s\-]*test |
    red[\s\-]*team | purple[\s\-]*team | vulnerability[\s\-]*(research|manage) |
    exploit[\s\-]*dev | security[\s\-]*engineer | security[\s\-]*researcher |
    secure[\s\-]*code | software[\s\-]*security
)""", re.IGNORECASE | re.VERBOSE)

TITLE_EXCLUDE = re.compile(r"""(
    \bsoc\b | security[\s\-]*operations | \bgrc\b | governance | compliance |
    audit | \biam\b | identity[\s\-]*and[\s\-]*access | privacy[\s\-]*counsel |
    physical[\s\-]*security | guard | sales[\s\-]*engineer |
    solutions[\s\-]*engineer | pre[\s\-]*sales | account[\s\-]*executive |
    customer[\s\-]*success | recruiter | technical[\s\-]*writer |
    program[\s\-]*manager | product[\s\-]*manager | \bintern\b | internship |
    new[\s\-]*grad | university[\s\-]*grad | \bjunior\b | apprentice |
    \bprincipal\b | \bdistinguished\b | \bfellow\b | \bstaff\b | \bdirector\b |
    \bhead[\s\-]of\b | \bvp\b | vice[\s\-]*president | \bmanager\b |
    \bmanagement\b | \blead\b
)""", re.IGNORECASE | re.VERBOSE)


def title_decision(title):
    """Return (passed, reason). Exclude wins over include."""
    title = title or ""
    ex = TITLE_EXCLUDE.search(title)
    if ex:
        return False, f"title excluded: {ex.group(0).strip()!r}"
    inc = TITLE_INCLUDE.search(title)
    if not inc:
        return False, "title not in include list"
    return True, f"title include: {inc.group(0).strip()!r}"


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


def salary_decision(salary_max, floor):
    """Reject only when a number is stated and even the top of the range is under the floor."""
    if salary_max is None:
        return True, "no salary stated"
    if salary_max < floor:
        return False, f"posted salary max ${salary_max:,} < floor ${floor:,}"
    return True, f"posted salary max ${salary_max:,}"


def yoe_decision(yoe_min, max_yoe):
    if yoe_min is None:
        return True, "no YoE stated"
    if yoe_min > max_yoe:
        return False, f"min YoE {yoe_min} > {max_yoe}"
    return True, f"min YoE {yoe_min}"


# --- company tiers -----------------------------------------------------------

_SUFFIXES = re.compile(
    r"\b(inc|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|ag|sa|bv|holdings|group|technologies|labs)\b\.?",
    re.I)


def normalize_company(name):
    n = (name or "").lower().replace("&", " and ")
    n = _SUFFIXES.sub(" ", n)
    return re.sub(r"[^a-z0-9]", "", n)


def load_tiers(path=None):
    path = path or os.environ.get("JOBAGENT_COMPANIES",
                                  os.path.join(os.path.dirname(os.path.abspath(__file__)), "companies.yaml"))
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    tiers = {}
    for name, tier in (data.get("companies") or {}).items():
        tiers[normalize_company(str(name))] = int(tier)
    return tiers


def company_tier(company, tiers):
    """Tier for a company, or None if not listed."""
    return tiers.get(normalize_company(company))


def tier_decision(company, tiers, max_tier, unknown_tier):
    tier = company_tier(company, tiers)
    known = tier is not None
    effective = tier if known else unknown_tier
    label = f"tier {tier}" if known else f"unknown company (treated as tier {unknown_tier})"
    if effective > max_tier:
        return False, effective, f"{label} > max {max_tier}"
    return True, effective, label
