"""The two LLM stages.

A. triage: title + company + location only, many jobs per request. Removes titles that are
   clearly not offensive security work (or clearly the wrong level); generic security titles
   stay "maybe" so the judge can read the duties.
B. judge: full posting excerpt, as many jobs per request as the per-request token cap allows.
   The model reports labels - above all how much of the day-to-day work is hands-on attacking -
   and `decide` turns them into send / reject.

The candidate wants OFFENSIVE work only: penetration testing, red teaming, offensive security
engineering, vulnerability research / exploit development, or AppSec / product security roles
with real hands-on testing. Not wanted (2026-10-24 feedback): detection engineering, incident
response, threat hunting, threat intel, SOC, security tooling / platform building, cloud / infra
hardening, vulnerability management, GRC / compliance, AI/ML research.

A batch whose reply is unusable (LLMBadOutput) is split in half and retried, down to single
jobs; a single job that still fails is left for the next run.
"""
import re

from llm import LLMBadOutput, LLMStop, estimate_tokens, pack
from logutil import log

# role_type values that are offensive by definition
OFFENSIVE_ROLES = ("pentest", "redteam", "offsec", "vulnresearch")
# role_type values never wanted, whatever offensive_work says
EXCLUDED_ROLES = ("tooling", "detection_response", "threat_intel", "infra_cloud",
                  "vuln_management", "grc", "ai_research", "other")
SEND_OFFENSIVE_WORK = ("core", "significant")
MAX_BAD_REPLIES = 8              # per stage per run, then leave the rest for the next run

TRIAGE_SYSTEM = """You screen LinkedIn job titles for one candidate who wants OFFENSIVE security work \
only: penetration testing, red teaming, offensive security engineering, vulnerability research, \
exploit development, and application / product security roles that involve hands-on security \
testing (finding and exploiting bugs, not building tooling).

Answer per job:
- "yes": title clearly names an offensive role: pentester / penetration tester, red team, \
offensive security, vulnerability researcher, exploit developer, security researcher (offensive), \
application security engineer, product security engineer.
- "maybe": a generic title that could be offensive work: "Security Engineer", "Software Security \
Engineer", "Security Consultant", "Security Assessment", unusual wording. When in doubt, "maybe".
- "no": clearly NOT offensive work: detection engineering, detection & response, incident response, \
threat hunting, threat intelligence / threat investigator, SOC / security operations / analyst, \
security tooling / platform / infrastructure / automation engineer, cloud / network / IT / \
endpoint / data security, vulnerability management, GRC / compliance / audit / risk / RMF, IAM, \
security architect, AI / ML scientist or research scientist, privacy, physical security, sales or \
solutions engineering, non-security roles; OR clearly the wrong level: intern, new grad, junior, \
apprentice, staff, principal, distinguished, fellow, lead, manager, director, head of, VP, CISO.

Return JSON only: {"results": [{"id": <id>, "t": "yes"|"maybe"|"no"}, ...]} with every id."""

JUDGE_SYSTEM = """You evaluate job postings for one candidate who wants OFFENSIVE security work only. \
For each job decide from the posting text (and the title/company when the text is missing).

1. role_type: what the job mainly is, judged by the duties, not the title. One of:
pentest, redteam, offsec, vulnresearch, appsec, prodsec, tooling, detection_response, \
threat_intel, infra_cloud, vuln_management, grc, ai_research, other.
- pentest: penetration testing of apps, networks, cloud, hardware.
- redteam: red team operations, adversary emulation, purple teaming led from the attack side.
- offsec: offensive security engineering (building AND using attack tooling, exploitation).
- vulnresearch: finding and exploiting vulnerabilities in software/hardware, exploit development, \
reverse engineering for bugs.
- appsec / prodsec: securing an organisation's applications or products.
- tooling: building security tools, platforms, scanners, SAST/DAST/SCA pipelines, guardrails, \
security infrastructure (KMS, auth), automation.
- detection_response: detection engineering, incident response, threat hunting, SOC.
- threat_intel: threat intelligence, threat investigations, malware tracking.
- infra_cloud: cloud / network / infrastructure / endpoint / corporate security hardening.
- vuln_management: scanning, patch tracking, vulnerability metrics and remediation programs.
- grc: governance, risk, compliance, audit, RMF, FedRAMP.
- ai_research: AI/ML research, model safety, benchmarks and evaluations, ML for security.

2. offensive_work: how much of the day-to-day job is hands-on attacking - finding and exploiting \
weaknesses. One of:
- "core": attacking IS the job (pentests, red team engagements, vuln research / exploit dev).
- "significant": a substantial, explicitly stated part of the duties is hands-on testing: manual \
penetration testing of apps / APIs / mobile / cloud, security assessments or code reviews where the \
engineer hunts and exploits bugs, bug bounty triage and reproduction. Threat modeling or design \
reviews alone do NOT count.
- "minor": testing is mentioned only in passing ("light pentests", "occasionally"); the job is \
mainly something else.
- "none": no hands-on attacking (tooling, detection, IR, infra, compliance, research...).
- "unclear": only when the posting text is unavailable.
AI red teaming counts as core / significant only when it is hands-on adversarial testing of systems \
or models (developing attacks, exploitation); evaluation design, benchmarks or safety research do not.
offensive_evidence: max 15 words naming the duties that justify offensive_work.

3. seniority_fit: is the ROLE's level right for the candidate, who has 2-5 years of experience and \
wants mid to senior level?
- "good": the role asks for roughly 2-5 years (a stated minimum of 2, 3, 4 or 5 counts), or is mid/senior.
- "role_too_junior": the role is BELOW the candidate: intern, new grad, entry level, 0-1 years.
- "role_too_senior": the role is ABOVE the candidate: minimum 7+ years, or staff/principal/lead/\
manager/director scope ("define org-wide strategy", "lead a team of").
- "unclear": not enough information.
If the posting lists several bars, the lowest minimum is the one that applies. yoe_required is that \
lowest minimum number of years, or null if none is stated.

4. comp_fit: would THIS company pay more than $250k total annual compensation (base + bonus + \
equity) for a mid-to-senior security engineer with 2-5 years of experience? Use what you know about \
the company's pay. A posted range is base salary only: at big tech, trading firms and well-funded \
startups total comp is often 1.3-2x base, so a posted base under $250k is NOT by itself a reason \
for "likely_below". "likely_below" only when you know this company's total comp at this level is \
below $250k (most consultancies, government contractors, non-tech enterprises); "likely_above" \
when you know it is above; otherwise "unclear". Staffing / recruiting firms hide the employer: \
"unclear". Never invent salary or equity figures; comp_note says what your judgment rests on.

reason: one clause, max 12 words, stating why the job should or should not be sent.
summary: 2-3 sentences on what the role does day to day, plus anything notable (clearance, \
on-site days, unusual stack). Plain text.

Return JSON only:
{"results": [{"id": <id>, "role_type": "...", "offensive_work": "...", "offensive_evidence": "...", \
"seniority_fit": "...", "yoe_required": <int or null>, "comp_fit": "...", "comp_note": "<max 15 words>", \
"reason": "...", "confidence": <0.0-1.0>, "summary": "..."}, ...]} with every id."""

TRIAGE_HEADER = "id | title | company | location\n"
TRIAGE_OUT_PER_JOB = 14
JUDGE_OUT_PER_JOB = 240
EXCERPT_KEYWORDS = re.compile(
    r"year|experience|qualif|require|salary|compensation|pay range|base pay|\$|equity|bonus|"
    r"clearance|on-?site|hybrid|remote|level|senior|staff|principal|lead|"
    r"pen(etration)?[ -]?test|red[ -]?team|offensive|exploit|vulnerab|attack|adversar|assess|"
    r"review|reverse|fuzz|bug|tool|automat|detect|incident|hunt|monitor|complian|build", re.I)


def excerpt(description, limit):
    """Fit a description into `limit` chars, keeping the opening (what the role is) and every
    line that mentions experience, pay, level, clearance, location, or the kind of work
    (testing / exploiting vs building / detecting / compliance)."""
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
    """Labels -> verdict.

    Offensive fit is required: core or significant hands-on attacking, or - when the posting text
    was unavailable - an offensive role type. Excluded role types never send. Seniority and pay
    reject only on a definite negative ("unclear" sends). A seniority rejection is ignored when the
    model's own yoe_required sits inside the candidate's band (seen in practice: "5+ years"
    labelled too junior, meaning the candidate rather than the role)."""
    role = v.get("role_type") or v.get("discipline")
    work = v.get("offensive_work")
    if role in EXCLUDED_ROLES:
        return "reject"
    if work not in SEND_OFFENSIVE_WORK and not (work == "unclear" and role in OFFENSIVE_ROLES):
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


def _run(stage, batches, call, handle):
    """Send batches in order. An unusable reply splits its batch in half (retried first); a single
    job that still fails is skipped and stays queued for the next run. LLMStop ends the stage."""
    queue = list(batches)
    bad = 0
    while queue:
        batch = queue.pop(0)
        try:
            reply = call(batch)
        except LLMStop:
            return
        except LLMBadOutput as e:
            bad += 1
            if bad > MAX_BAD_REPLIES:
                log(f"{stage}: {bad} unusable replies this run, leaving the rest for the next run")
                return
            if len(batch) > 1:
                half = len(batch) // 2
                log(f"{stage}: unusable reply for {len(batch)} jobs ({e}); retrying as "
                    f"{half} + {len(batch) - half}")
                queue[:0] = [batch[:half], batch[half:]]
            else:
                log(f"{stage}: unusable reply for a single job ({e}); it stays queued for next run")
            continue
        handle(batch, reply)


def _triage_line(i, j):
    return f'{i} | {j.get("title") or "?"} | {j.get("company") or "?"} | {j.get("location") or "?"}'


def triage(client, jobs, cfg, on_result):
    """Label jobs yes/maybe/no. Calls on_result(job, label) as each batch returns; jobs the
    model leaves out of its reply default to "maybe". Stops quietly on LLMStop."""
    reasoning = int(cfg.get("reasoning_tokens", 0))
    # Same fixed costs chat() will count: header line, +30 in max_tokens, reasoning headroom.
    overhead = estimate_tokens(TRIAGE_SYSTEM) + estimate_tokens(TRIAGE_HEADER) + 30 + reasoning
    batches = pack(jobs, lambda j: estimate_tokens(_triage_line(0, j)) + 2, overhead,
                   TRIAGE_OUT_PER_JOB, client.request_max, int(cfg.get("triage_batch_max", 80)))
    done = [0]

    def call(batch):
        lines = "\n".join(_triage_line(i, j) for i, j in enumerate(batch, 1))
        return client.chat(TRIAGE_SYSTEM, TRIAGE_HEADER + lines,
                           max_tokens=TRIAGE_OUT_PER_JOB * len(batch) + 30 + reasoning)

    def handle(batch, reply):
        res = _results(reply)
        if len(res) < len(batch):
            log(f"triage: reply covered {len(res)} of {len(batch)} jobs, the rest count as maybe")
        for i, j in enumerate(batch, 1):
            label = (res.get(str(i)) or {}).get("t")
            on_result(j, label if label in ("yes", "maybe", "no") else "maybe")
            done[0] += 1

    _run("triage", batches, call, handle)
    return done[0]


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
    """Full judgment. Calls on_result(job, verdict_dict) per job. Jobs missing from a reply, or in
    a batch that never produced a usable reply, are left unjudged (retried next run)."""
    if not jobs:
        return 0
    desc_chars = int(cfg.get("judge_desc_chars", 3500))
    reasoning = int(cfg.get("reasoning_tokens", 0))
    # Same fixed costs chat() will count: +60 in max_tokens, reasoning headroom, and slack for
    # the newlines joining the job blocks.
    overhead = estimate_tokens(JUDGE_SYSTEM) + 60 + 20 + reasoning
    # One job must always fit in a request: shrink the excerpt if the per-request cap is tight.
    room = client.request_max - overhead - JUDGE_OUT_PER_JOB - 150
    # And all of today's jobs should fit in what is left of the daily token budget: spread it,
    # assuming ~3 jobs share each request's overhead. Below 800 chars we keep 800 and let the
    # budget stop the run; the rest wait for tomorrow.
    per_job = client.tokens_left_today() / len(jobs) - JUDGE_OUT_PER_JOB - overhead / 3 - 60
    desc_chars = int(max(800, min(desc_chars, room * 3, per_job * 3)))
    batches = pack(jobs, lambda j: estimate_tokens(_judge_block(0, j, desc_chars)), overhead,
                   JUDGE_OUT_PER_JOB, client.request_max, int(cfg.get("judge_batch_max", 10)))
    done = [0]

    def call(batch):
        body = "\n".join(_judge_block(i, j, desc_chars) for i, j in enumerate(batch, 1))
        return client.chat(JUDGE_SYSTEM, body,
                           max_tokens=JUDGE_OUT_PER_JOB * len(batch) + 60 + reasoning)

    def handle(batch, reply):
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
            done[0] += 1

    _run("judge", batches, call, handle)
    return done[0]
