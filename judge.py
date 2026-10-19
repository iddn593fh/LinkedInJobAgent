"""The two LLM stages.

A. triage: title + company + location only, many jobs per request. Leans to "maybe": it only
   removes jobs that are clearly not the target discipline or clearly the wrong level.
B. judge: full posting excerpt, as many jobs per request as the per-request token cap allows.
   The model reports discipline / seniority / pay as labels; `decide` turns the labels into
   send / reject, so "unclear" always means send no matter how the model phrases it.
"""
import re

from llm import LLMStop, estimate_tokens, pack
from logutil import log

DISCIPLINES = ("appsec", "prodsec", "offsec", "pentest", "redteam", "vulnresearch")

TRIAGE_SYSTEM = """You screen LinkedIn job titles for one candidate. Target roles: application security, \
product security, offensive security, penetration testing, red teaming, vulnerability research, \
exploit development, secure code review - including generic "Security Engineer" / "Security \
Researcher" titles that could be any of these.

Answer per job:
- "yes": title clearly names a target role.
- "maybe": could be a target role (generic security title, unusual wording, security work inside \
a software/research title). When in doubt, "maybe".
- "no": clearly something else: SOC / detection & response / incident response / threat intel, \
GRC / compliance / audit / risk, IAM, network/cloud/IT security operations, security sales or \
solutions engineering, physical security, privacy, non-security roles; OR clearly the wrong \
level: intern, new grad, junior, apprentice, staff, principal, distinguished, fellow, lead, \
manager, director, head of, VP, CISO.

Return JSON only: {"results": [{"id": <id>, "t": "yes"|"maybe"|"no"}, ...]} with every id."""

JUDGE_SYSTEM = """You evaluate job postings for one candidate. For each job decide three things \
from the posting text (and the title/company when the text is missing or thin).

1. discipline: the role's actual day-to-day work. One of appsec, prodsec, offsec, pentest, \
redteam, vulnresearch, other. "Security Engineer" can be AppSec at one company and SOC tooling \
at another - judge by the duties. Detection & response, SOC, GRC, IAM, cloud/network/IT security \
operations, sales engineering, physical security and privacy are "other".

2. seniority_fit: is the ROLE's level right for the candidate, who has 2-5 years of experience and \
wants mid to senior level?
- "good": the role asks for roughly 2-5 years (a stated minimum of 2, 3, 4 or 5 counts), or is mid/senior.
- "role_too_junior": the role is BELOW the candidate: intern, new grad, entry level, 0-1 years.
- "role_too_senior": the role is ABOVE the candidate: minimum 7+ years, or staff/principal/lead/\
manager/director scope ("define org-wide strategy", "lead a team of").
- "unclear": not enough information.
If the posting lists several bars, the lowest minimum is the one that applies. yoe_required is that \
lowest minimum number of years, or null if none is stated.

3. comp_fit: would THIS company pay more than $250k total annual compensation (base + bonus + \
equity) for a mid-to-senior security engineer with 2-5 years of experience, at this level? Use \
what you know about the company's pay. A posted range is base salary only: at big tech, trading \
firms and well-funded startups, total comp is often 1.3-2x base, so a posted base under $250k is NOT \
by itself a reason for "likely_below". Answer "likely_below" only when you know this company's total \
comp at this level is below $250k (e.g. most consultancies, government contractors, non-tech \
enterprises), "likely_above" when you know it is above, otherwise "unclear". Never invent salary or \
equity figures; comp_note says what your judgment rests on.

"unclear" is always an allowed answer: a false positive costs the candidate ten seconds, a false \
negative costs an opportunity.

summary: 2-3 sentences on what the role does day to day, plus anything notable (clearance, \
on-site days, unusual stack). Plain text.

Return JSON only:
{"results": [{"id": <id>, "discipline": "...", "seniority_fit": "...", "yoe_required": <int or null>, \
"comp_fit": "...", "comp_note": "<max 15 words>", "reason": "<one clause, max 12 words>", \
"confidence": <0.0-1.0>, "summary": "..."}, ...]} with every id."""

TRIAGE_OUT_PER_JOB = 14
JUDGE_OUT_PER_JOB = 190
EXCERPT_KEYWORDS = re.compile(
    r"year|experience|qualif|require|salary|compensation|pay range|base pay|\$|equity|bonus|"
    r"clearance|on-?site|hybrid|remote|level|senior|staff|principal|lead", re.I)


def excerpt(description, limit):
    """Fit a description into `limit` chars, keeping the opening (what the role is) and every
    line that mentions experience, pay, level, clearance or location."""
    text = (description or "").strip()
    if len(text) <= limit:
        return text
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    keep = [False] * len(lines)
    budget = limit
    for i, l in enumerate(lines):                     # opening paragraph(s) first
        if budget - len(l) < limit * 0.4:
            break
        keep[i] = True
        budget -= len(l) + 1
    for i, l in enumerate(lines):                     # then the lines that settle the judgment
        if not keep[i] and EXCERPT_KEYWORDS.search(l) and len(l) + 1 <= budget:
            keep[i] = True
            budget -= len(l) + 1
    out, gap = [], False
    for k, l in zip(keep, lines):
        if k:
            out.append(l)
            gap = False
        elif not gap:
            out.append("[...]")
            gap = True
    return "\n".join(out)[:limit]


CANDIDATE_YOE = (2, 5)


def decide(v):
    """Labels -> verdict. Only a definite negative on any axis rejects.

    A seniority rejection is ignored when the model's own yoe_required sits inside the
    candidate's band: the label is then contradicting the number (seen in practice: "5+ years"
    labelled too junior, meaning the candidate rather than the role)."""
    if v.get("discipline") not in DISCIPLINES:
        return "reject"
    if v.get("seniority_fit") in ("role_too_junior", "role_too_senior", "too_junior", "too_senior"):
        yoe = v.get("yoe_required")
        in_band = isinstance(yoe, (int, float)) and CANDIDATE_YOE[0] <= yoe <= CANDIDATE_YOE[1]
        if not in_band:
            return "reject"
    if v.get("comp_fit") == "likely_below":
        return "reject"
    return "send"


def _results(reply):
    rows = reply.get("results") if isinstance(reply, dict) else None
    return {str(r.get("id")): r for r in rows or [] if isinstance(r, dict)}


def _triage_line(i, j):
    return f'{i} | {j.get("title") or "?"} | {j.get("company") or "?"} | {j.get("location") or "?"}'


def triage(client, jobs, cfg, on_result):
    """Label jobs yes/maybe/no. Calls on_result(job, label) as each batch returns; jobs the
    model leaves out of its reply default to "maybe". Stops quietly on LLMStop."""
    reasoning = int(cfg.get("reasoning_tokens", 0))
    overhead = estimate_tokens(TRIAGE_SYSTEM) + 20 + reasoning
    batches = pack(jobs, lambda j: estimate_tokens(_triage_line(0, j)) + 2, overhead,
                   TRIAGE_OUT_PER_JOB, client.request_max, int(cfg.get("triage_batch_max", 80)))
    done = 0
    for batch in batches:
        lines = "\n".join(_triage_line(i, j) for i, j in enumerate(batch, 1))
        try:
            reply = client.chat(TRIAGE_SYSTEM, "id | title | company | location\n" + lines,
                                max_tokens=TRIAGE_OUT_PER_JOB * len(batch) + 30 + reasoning)
        except LLMStop:
            break
        res = _results(reply)
        if len(res) < len(batch):
            log(f"triage: reply covered {len(res)} of {len(batch)} jobs, the rest count as maybe")
        for i, j in enumerate(batch, 1):
            label = (res.get(str(i)) or {}).get("t")
            on_result(j, label if label in ("yes", "maybe", "no") else "maybe")
            done += 1
    return done


def _judge_block(i, j, desc_chars):
    hints = []
    if j.get("seniority_level"):
        hints.append(f"LinkedIn seniority label: {j['seniority_level']}")
    if j.get("yoe_min") is not None:
        hints.append(f"parsed minimum years: {j['yoe_min']}")
    if j.get("salary_max"):
        hints.append(f"posted salary: ${j['salary_min']:,}-${j['salary_max']:,}")
    elif j.get("salary_text"):
        hints.append(f"posted salary: {j['salary_text']}")
    desc = excerpt(j.get("description"), desc_chars) if j.get("description") else "(posting text unavailable)"
    return (f"### id {i}\ntitle: {j.get('title')}\ncompany: {j.get('company')}\n"
            f"location: {j.get('location')}\n" + "".join(h + "\n" for h in hints) + f"posting:\n{desc}\n")


def judge(client, jobs, cfg, on_result):
    """Full judgment. Calls on_result(job, verdict_dict) per job. Jobs missing from a reply are
    left unjudged (retried next run). Stops quietly on LLMStop."""
    if not jobs:
        return 0
    desc_chars = int(cfg.get("judge_desc_chars", 3500))
    reasoning = int(cfg.get("reasoning_tokens", 0))
    overhead = estimate_tokens(JUDGE_SYSTEM) + 20 + reasoning
    # One job must always fit in a request: shrink the excerpt if the per-request cap is tight.
    room = client.request_max - overhead - JUDGE_OUT_PER_JOB - 150
    # And all of today's jobs should fit in what is left of the daily token budget: spread it,
    # assuming ~3 jobs share each request's overhead. Below 800 chars we keep 800 and let the
    # budget stop the run; the rest wait for tomorrow.
    per_job = client.tokens_left_today() / len(jobs) - JUDGE_OUT_PER_JOB - overhead / 3 - 60
    desc_chars = int(max(800, min(desc_chars, room * 3, per_job * 3)))
    batches = pack(jobs, lambda j: estimate_tokens(_judge_block(0, j, desc_chars)), overhead,
                   JUDGE_OUT_PER_JOB, client.request_max, int(cfg.get("judge_batch_max", 10)))
    done = 0
    for batch in batches:
        body = "\n".join(_judge_block(i, j, desc_chars) for i, j in enumerate(batch, 1))
        try:
            reply = client.chat(JUDGE_SYSTEM, body,
                                max_tokens=JUDGE_OUT_PER_JOB * len(batch) + 60 + reasoning)
        except LLMStop:
            break
        res = _results(reply)
        if len(res) < len(batch):
            log(f"judge: reply covered {len(res)} of {len(batch)} jobs, the rest retry next run")
        for i, j in enumerate(batch, 1):
            v = res.get(str(i))
            if not v:
                continue
            v.pop("id", None)
            v["verdict"] = decide(v)
            v["judged_on"] = "posting" if j.get("description") else "title only"
            on_result(j, v)
            done += 1
    return done
