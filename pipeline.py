"""Per-card deterministic filtering shared by `debug` and `fetch`.

Stage order (cheap and local first): title -> dedup -> tier -> salary/YoE prefilter.
Dedup runs before anything that could cost a network request.
"""
import db
import filters
from filters import parse_salary


def evaluate_card(card, conn, tiers, opts, seen_this_run):
    """Annotate a parsed card with fingerprint/tier/salary and a decision.

    Returns the card dict extended with: fingerprint, tier, salary_min, salary_max,
    yoe_min, stage (furthest stage passed), passed (bool), reason.
    """
    c = dict(card)
    c["fingerprint"] = filters.fingerprint(c.get("company"), c.get("title"), c.get("location"))
    c["salary_min"], c["salary_max"] = parse_salary(c.get("salary_text"))
    c.setdefault("yoe_min", None)
    c["tier"] = None
    c["warnings"] = []
    if not c.get("company"):
        c["warnings"].append("no company parsed")
    if not c.get("location"):
        c["warnings"].append("no location parsed")

    def result(stage, passed, reason):
        c.update(stage=stage, passed=passed, reason=reason)
        return c

    ok, why = filters.title_decision(c.get("title"))
    if not ok:
        return result("title", False, why)

    if c["job_id"] in seen_this_run or c["fingerprint"] in seen_this_run:
        return result("dedup", False, "duplicate within this run")
    seen_this_run.update((c["job_id"], c["fingerprint"]))
    if db.is_notified(conn, c["job_id"], c["fingerprint"]):
        return result("dedup", False, "already sent (job ID or fingerprint)")

    ok, c["tier"], why = filters.tier_decision(c.get("company"), tiers, opts.max_tier, opts.unknown_tier)
    if not ok:
        return result("tier", False, why)

    ok, why_sal = filters.salary_decision(c["salary_max"], opts.base_floor)
    if not ok:
        return result("prefilter", False, why_sal)

    return result("prefilter", True, why)


def apply_yoe(c, opts):
    """Second prefilter pass once enrichment has supplied yoe_min / salary."""
    ok, why = filters.salary_decision(c.get("salary_max"), opts.base_floor)
    if not ok:
        c.update(passed=False, stage="prefilter", reason=why)
        return c
    ok, why = filters.yoe_decision(c.get("yoe_min"), opts.max_yoe)
    if not ok:
        c.update(passed=False, stage="prefilter", reason=why)
    return c
