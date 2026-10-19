"""Full result lists from LinkedIn's guest job search, driven by searches.yaml.

Alert emails show ~6 cards out of "30+ new jobs"; the hidden ones are not in the email at
all. The guest search endpoint (what logged-out /jobs/search pages page through) returns
every match, 10 per page, as the same title / company / location card.
"""
import os
import re

import yaml
from bs4 import BeautifulSoup

from email_parser import canonical_url, job_id_from_href
from linkedin_http import Stop
from logutil import log

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
PAGE_SIZE = 10
URN_RE = re.compile(r"jobPosting:(\d+)")


def _text(node):
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node else None


def parse_search_html(html):
    """Cards from one page of guest search results: job_id, title, company, location,
    salary_text, posted, url."""
    soup = BeautifulSoup(html or "", "html.parser")
    cards = []
    for card in soup.select(".base-search-card, .job-search-card"):
        jid = None
        m = URN_RE.search(card.get("data-entity-urn") or "")
        if m:
            jid = m.group(1)
        else:
            a = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
            jid = job_id_from_href(a["href"]) if a else None
        title = _text(card.select_one(".base-search-card__title"))
        if not jid or not title:
            continue
        posted = card.select_one("time")
        cards.append({
            "job_id": jid,
            "title": title,
            "company": _text(card.select_one(".base-search-card__subtitle")),
            "location": _text(card.select_one(".job-search-card__location")),
            "salary_text": _text(card.select_one(".job-search-card__salary-info")),
            "posted": posted.get("datetime") if posted else None,
            "url": canonical_url(jid),
        })
    # One card element can match both selectors; keep the first per ID.
    seen, out = set(), []
    for c in cards:
        if c["job_id"] not in seen:
            seen.add(c["job_id"])
            out.append(c)
    return out


def load_searches(path=None):
    path = path or os.environ.get("JOBAGENT_SEARCHES",
                                  os.path.join(os.path.dirname(os.path.abspath(__file__)), "searches.yaml"))
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    defaults = data.get("defaults") or {}
    return [{**defaults, **s} for s in (data.get("searches") or [])]


def search_params(search, hours, start):
    p = {"keywords": search["keywords"], "f_TPR": f"r{int(hours) * 3600}", "start": start}
    if search.get("geo_id"):
        p["geoId"] = str(search["geo_id"])
    elif search.get("location"):
        p["location"] = search["location"]
    if search.get("remote"):
        p["f_WT"] = "2"
    return p


def run_searches(client, searches, hours, max_pages, search_delay=None):
    """Page through each search until it runs dry. Returns (cards, per_search).

    A search ends on a short page, a page with no job IDs not already seen in this search
    (LinkedIn sometimes repeats results), or max_pages (a search may set its own max_pages).
    Results are relevance-ranked and broad terms rarely run dry, so the cap is what usually
    ends it. Before the first page of every search after the first, wait search_delay instead
    of the client's page delay. Any Stop (block, time budget) ends all searching; cards
    gathered so far are kept.
    """
    cards, per_search = [], []
    for i, s in enumerate(searches):
        info = {"keywords": s["keywords"], "pages": 0, "cards": 0, "capped": False, "stopped": None}
        per_search.append(info)
        ids = set()
        for page in range(int(s.get("max_pages", max_pages))):
            delay = search_delay if (page == 0 and i > 0) else None
            try:
                html = client.get(SEARCH_URL, search_params(s, hours, page * PAGE_SIZE), delay=delay)
            except Stop:
                info["stopped"] = client.stop_reason()
                for rest in searches[i + 1:]:
                    per_search.append({"keywords": rest["keywords"], "pages": 0, "cards": 0,
                                       "capped": False, "stopped": "not run"})
                return cards, per_search
            if html is None:
                if client.throttled:
                    info["throttled"] = True   # rest of this search skipped after 429s
                break
            page_cards = parse_search_html(html)
            got = [c for c in page_cards if c["job_id"] not in ids]
            info["pages"] += 1
            for c in got:
                ids.add(c["job_id"])
                c["source"] = f"search:{s['keywords']}"
            cards.extend(got)
            info["cards"] += len(got)
            if len(page_cards) < PAGE_SIZE or not got:
                break
        else:
            info["capped"] = True      # hit max_pages: more results may exist
        log(f"search {s['keywords']!r} done: {info['cards']} cards in {info['pages']} page(s)")
    return cards, per_search
