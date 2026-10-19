"""Full posting text from LinkedIn's guest endpoint: description, YoE, salary, seniority."""
import re

from bs4 import BeautifulSoup

from parsing import parse_salary, parse_yoe

POSTING_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"
DESCRIPTION_CHARS = 6000


def parse_posting_html(html):
    soup = BeautifulSoup(html or "", "html.parser")
    desc_node = soup.select_one(".show-more-less-html__markup, .description__text")
    description = desc_node.get_text("\n", strip=True) if desc_node else ""

    criteria = {}
    for item in soup.select(".description__job-criteria-item"):
        k = item.select_one(".description__job-criteria-subheader")
        v = item.select_one(".description__job-criteria-text")
        if k and v:
            criteria[k.get_text(strip=True).lower()] = v.get_text(strip=True)

    # Salary: a top-card compensation block when LinkedIn shows one, else the description.
    # Never the whole page, so figures from unrelated widgets can't leak in.
    comp = " ".join(n.get_text(" ", strip=True)
                    for n in soup.find_all(class_=re.compile(r"salary|compensation")))
    smin, smax = parse_salary(comp)
    if smin is None:
        smin, smax = parse_salary(description)

    return {
        "description": description[:DESCRIPTION_CHARS],
        "yoe_min": parse_yoe(description),
        "salary_min": smin,
        "salary_max": smax,
        "seniority_level": criteria.get("seniority level"),
    }


def fetch_posting(client, job_id):
    """Parsed posting, or None if it could not be fetched (closed, 404).
    Raises linkedin_http.Stop when LinkedIn blocks or the time budget is spent."""
    html = client.get(POSTING_URL.format(job_id))
    return parse_posting_html(html) if html else None
