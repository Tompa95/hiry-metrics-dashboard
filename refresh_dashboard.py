#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hiry client-metrics dashboard refresher.

Self-contained (standard library only). Pulls live Airtable data (READ-ONLY),
computes every metric per the client-metrics skill, builds a single-file HTML
dashboard matching the locked design, and publishes it to GitHub Pages by
overwriting index.html in the target repo via the GitHub REST API.

The ONLY write this script makes is the GitHub index.html commit.
No Airtable writes, no Slack, no CSM tag write-back.

Run by macOS launchd once a day.
"""

import os
import sys
import json
import time
import base64
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, date, timezone

# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "dashboard_config.json")
LOG_PATH = os.path.join(HERE, "refresh.log")
# Archive location for the daily HTML copy. The runner lives outside the vault
# (macOS TCC blocks launchd from ~/Desktop), so the relative path would resolve
# to the home folder. run_daily.sh sets this explicitly to the vault folder.
DASHBOARDS_DIR = (os.environ.get("HIRY_DASHBOARDS_DIR")
                  or os.path.abspath(os.path.join(HERE, "..", "01 Dashboards")))

TODAY = date.today()  # today is 2026-07-02 per the run environment


def log(msg):
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config():
    """Credentials come from the environment first (cloud routine), falling back
    to the local dashboard_config.json (Tom's Mac). The config file is NEVER
    committed to the repo, only this script is."""
    env = {
        "airtable_pat": os.environ.get("HIRY_AIRTABLE_PAT"),
        "github_pat": os.environ.get("HIRY_GITHUB_PAT"),
        "base": os.environ.get("HIRY_BASE", "app4grbqtlDH4GUvP"),
        "repo": os.environ.get("HIRY_REPO", "Tompa95/hiry-metrics-dashboard"),
    }
    if env["airtable_pat"] and env["github_pat"]:
        log("Config: using environment variables")
        return env
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        log("Config: using local dashboard_config.json")
        return json.load(f)


# ---------------------------------------------------------------------------
# Airtable REST (read-only)
# ---------------------------------------------------------------------------
class Airtable:
    def __init__(self, pat, base):
        self.pat = pat
        self.base = base

    def _get(self, url):
        for attempt in range(6):
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.pat})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(1.0 + attempt)
                    continue
                raise
            except urllib.error.URLError:
                time.sleep(1.0 + attempt)
        raise RuntimeError("Airtable GET failed after retries: " + url)

    def list_all(self, table, fields=None, filter_formula=None):
        """Paginate a whole table (READ-ONLY)."""
        records = []
        offset = None
        while True:
            params = [("pageSize", "100")]
            if fields:
                for fld in fields:
                    params.append(("fields[]", fld))
            if filter_formula:
                params.append(("filterByFormula", filter_formula))
            if offset:
                params.append(("offset", offset))
            qs = urllib.parse.urlencode(params, doseq=True)
            url = "https://api.airtable.com/v0/%s/%s?%s" % (self.base, table, qs)
            data = self._get(url)
            records.extend(data.get("records", []))
            offset = data.get("offset")
            if not offset:
                break
        return records


# ---------------------------------------------------------------------------
# Table + field IDs / names
# ---------------------------------------------------------------------------
T_CLIENTS = "tbl1DoWD8gFSarKwV"
T_PLACEMENTS = "tblbzOfPiDGP8iLJt"
T_MATCHES = "tblUKHOCzJy5wg5ZU"

F_CLIENT_NAME = "ℹ️ Name"
F_CLIENT_FULFILL = "Fulfillment Status"

F_PL_STATUS = "Fulfilment Status"
F_PL_HEADLINE = "ℹ️ Headline"
F_PL_CLIENT = "ℹ️ Client"
F_PL_CSMTAGS = "CSM Tags"
F_PL_CLOSED_TS = "TRACKING - last modified fulfillment status (closed)"
F_PL_CLOSED_FALLBACK = "Closed Date (tracking)"
F_PL_CHURN_DATE = "Churn Date (MANUAL)"

F_M_STATUS = "Candidate Status"
F_M_RATING = "Candidate Rating"
F_M_DAYS = "\U0001f5d3️ Days Since Status Changed"
F_M_NAME = "ℹ️ Name"
F_M_POSITION = "Position"

# ---------------------------------------------------------------------------
# Status sets (names, since REST returns names)
# ---------------------------------------------------------------------------
ACTIVE_PLACEMENT_STATUSES = {
    "05 - Pending/ In Planning",
    "10 - Sourcing To Start",
    "20 - Sourcing Completed",
    "30 - Matching To Start",
    "35 - Matching Completed",
    "40 - Feedback Waiting - Candidate",
    "50 - Feedback Waiting - Client",
    "60 - In Interviews",
    "70 - In Trial",
    "90 - In Final Selection",
    "source",
    "05 - Sourcing",
    "Pending/In Planning",
}

REJECTED_RATING = "Candidate - Rejected"

# Viable / live-pipeline candidate statuses (from Matching Priorities skill)
VIABLE_STATUSES = {
    "08 - Introduced",
    "081 - Intro sent: full transcript",
    "09 - Interview - To Schedule",
    "10 - Interview - Scheduled",
    "11 - Interview - Completed",
    "12 - Negotiation",
    "13 - Trial To Start",
    "13 - Trial In Progress",
    "14 - Trial Completed",
    "13 - Assessment To Send",
    "14 - Assessment Shared",
    "15 - Assessment In Progress",
    "16 - Assessment Completed",
}

# Funnel buckets
FUNNEL_INTRODUCED = {"08 - Introduced", "081 - Intro sent: full transcript"}
FUNNEL_INTERVIEW = {"09 - Interview - To Schedule", "10 - Interview - Scheduled", "11 - Interview - Completed"}
FUNNEL_ASSESSMENT = {"13 - Assessment To Send", "14 - Assessment Shared", "15 - Assessment In Progress", "16 - Assessment Completed"}
FUNNEL_TRIAL = {"13 - Trial To Start", "13 - Trial In Progress", "14 - Trial Completed"}
FUNNEL_NEGOTIATION = {"12 - Negotiation"}
FUNNEL_HIRED = {"17 - Hired"}
# Everything else viable-or-sourcing that is not one of the above and not dead = Sourcing

# Interview tab sections (ordered earliest -> latest)
INTERVIEW_SECTIONS = [
    ("Interview to schedule", "tosched", "09 - Interview - To Schedule"),
    ("Interview scheduled", "sched", "10 - Interview - Scheduled"),
    ("Interview completed", "done", "11 - Interview - Completed"),
]
ASSESSMENT_SECTIONS = [
    ("Assessment to send", "tosched", "13 - Assessment To Send"),
    ("Assessment shared", "sched", "14 - Assessment Shared"),
    ("Assessment in progress", "sched", "15 - Assessment In Progress"),
    ("Assessment completed", "done", "16 - Assessment Completed"),
]

# Client-health per-placement buckets (non-overlapping)
CH_MATCHED = {
    "00 - Offer Made", "01 - Follow Up Linkedin", "02 - Follow Up Email",
    "03 - Follow Up Slack/Whatsapp", "065 - Applied Direct", "07 - Verification",
    "0-78 - Waiting For Interview", "070", "071", "072", "15 - Paused",
}
CH_APPROVED = {"079 - Approved Internally", "06 - Offer Approved"}
CH_INTRO_AFTER = VIABLE_STATUSES | {"17 - Hired", "081"}
CH_REJECTED_STATUSES = {
    "16 - Internally Rejected", "04 - Offer Ghosted", "05 - Offer Rejected",
    "14 - Trial Failed", "17 - Assessment Ghosted", "18 - Assessment Failed",
}

# Shortage fresh pools (<14 days)
CLOSE_TO_INTRO_STATUSES = {"079 - Approved Internally", "07 - Verification", "0-78 - Waiting For Interview"}
ACTIVE_SOURCING_STATUSES = {
    "00 - Offer Made", "06 - Offer Approved", "01 - Follow Up Linkedin",
    "02 - Follow Up Email", "03 - Follow Up Slack/Whatsapp",
}

# Close-to-hire late stages
CLOSE_NEGOTIATION = "12 - Negotiation"
CLOSE_TRIAL = {"13 - Trial To Start", "13 - Trial In Progress", "14 - Trial Completed"}
CLOSE_INTERVIEW_DONE = "11 - Interview - Completed"

# Close to hire is driven by the ROLE's fulfilment status, not the candidate's
CLOSE_PLACEMENT_STATUSES = ["90 - In Final Selection", "70 - In Trial"]

# Candidate statuses that sit PAST 'interview completed' in pipeline order.
# Assessment -> Trial -> Negotiation -> Hired
PAST_INTERVIEW_STATUSES = {
    "13 - Assessment To Send", "14 - Assessment Shared",
    "15 - Assessment In Progress", "16 - Assessment Completed",
    "13 - Trial To Start", "13 - Trial In Progress", "14 - Trial Completed",
    "12 - Negotiation",
    "17 - Hired",
}

# Statuses to exclude from all stage counts (dead/lost candidates)
EXCLUDED_STATUSES = {"04 - Offer Ghosted", "05 - Offer Rejected"}

# Client Health stage breakdown (new)
CH_SOURCING = {
    "00 - Offer Made", "01 - Follow Up (Linkedin)", "02 - Follow Up (Email)",
    "03 - Follow Up (Whatsapp)", "06 - Offer Approved", "065 - Applied Direct",
    "07 - Verification", "0-78 - Waiting For Interview"
}
CH_APPROVED_INT = {"079 - Approved Internally"}
CH_INTRODUCED = {"08 - Introduced", "081 - Intro sent: full transcript", "070 - Intro waiting: no interview linked",
                  "071 - Intro waiting: no recording URL", "072 - Intro waiting: transcript pull pending"}
CH_INTERVIEW = {"09 - Interview - To Schedule", "10 - Interview - Scheduled", "11 - Interview - Completed"}
CH_ASSESSMENT = {
    "13 - Assessment To Send", "14 - Assessment Shared", "15 - Assessment In Progress",
    "16 - Assessment Completed", "17 - Assessment Ghosted", "18 - Assessment Failed"
}
CH_FINAL = {
    "12 - Negotiation", "13 - Trial To Start", "13 - Trial In Progress",
    "14 - Trial Completed", "14 - Trial Failed", "17 - Hired"
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def ordinal(n):
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return str(n) + suf


def pretty_date(d):
    """date -> 'June 30th' style."""
    return d.strftime("%B") + " " + ordinal(d.day)


def parse_date(val):
    if not val:
        return None
    s = str(val)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        try:
            return datetime.fromisoformat(str(val).replace("Z", "+00:00")).date()
        except Exception:
            return None


def esc(s):
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def link_id(field_val):
    """Return first linked record id or None."""
    if isinstance(field_val, list) and field_val:
        return field_val[0]
    return None


def days_of(rec_fields):
    v = rec_fields.get(F_M_DAYS)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def status_of(rec_fields):
    """Candidate Status, trimmed. Some Airtable choices carry a trailing space
    (e.g. '12 - Negotiation '), which silently broke every equality check."""
    v = rec_fields.get(F_M_STATUS)
    return v.strip() if isinstance(v, str) else v


def is_rejected(rec_fields):
    return rec_fields.get(F_M_RATING) == REJECTED_RATING


def name_of(rec_fields):
    v = rec_fields.get(F_M_NAME)
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def dot_for_days(d):
    if d is None:
        return ""
    if d > 14:
        return "\U0001f534"  # red
    if d > 7:
        return "\U0001f7e0"  # orange
    return ""


# ---------------------------------------------------------------------------
# Main compute
# ---------------------------------------------------------------------------
def main():
    cfg = load_config()
    at = Airtable(cfg["airtable_pat"], cfg["base"])

    log("Pulling Airtable data (read-only)...")

    # --- Clients: Fulfillment-Active only ---
    clients = at.list_all(
        T_CLIENTS,
        fields=[F_CLIENT_NAME, F_CLIENT_FULFILL],
        filter_formula="{%s}='Fulfillment - Active'" % F_CLIENT_FULFILL,
    )
    client_name = {}
    active_client_ids = set()
    for c in clients:
        client_name[c["id"]] = c["fields"].get(F_CLIENT_NAME, "(unnamed)")
        active_client_ids.add(c["id"])
    log("Fulfillment-Active clients pulled: %d" % len(active_client_ids))

    # --- Placements: pull all (need active + closed + churned for hires/churn) ---
    placements = at.list_all(
        T_PLACEMENTS,
        fields=[F_PL_STATUS, F_PL_HEADLINE, F_PL_CLIENT, F_PL_CSMTAGS,
                F_PL_CLOSED_TS, F_PL_CLOSED_FALLBACK, F_PL_CHURN_DATE],
    )
    log("Placements pulled (all): %d" % len(placements))

    active_placements = {}   # pid -> fields (with createdTime added)
    for p in placements:
        f = p["fields"]
        if f.get(F_PL_STATUS) in ACTIVE_PLACEMENT_STATUSES:
            f["_createdTime"] = p.get("createdTime")  # store createdTime as a special field
            active_placements[p["id"]] = f

    # Active client = Fulfillment-Active AND owns >=1 active placement
    clients_with_active = set()
    for pid, f in active_placements.items():
        cid = link_id(f.get(F_PL_CLIENT))
        if cid in active_client_ids:
            clients_with_active.add(cid)

    # keep only active placements whose client is active
    active_placements = {
        pid: f for pid, f in active_placements.items()
        if link_id(f.get(F_PL_CLIENT)) in active_client_ids
    }
    log("Active placements on active clients: %d" % len(active_placements))
    log("Active clients (Fulfillment-Active AND own active placement): %d" % len(clients_with_active))

    # --- Matches: pull all, keep only those on active placements ---
    matches = at.list_all(
        T_MATCHES,
        fields=[F_M_STATUS, F_M_RATING, F_M_DAYS, F_M_NAME, F_M_POSITION],
    )
    log("Matches pulled (all): %d" % len(matches))

    # bucket matches by placement id (active only)
    matches_by_pl = {pid: [] for pid in active_placements}
    for m in matches:
        f = m["fields"]
        pid = link_id(f.get(F_M_POSITION))
        if pid in matches_by_pl:
            matches_by_pl[pid].append(f)
    total_active_matches = sum(len(v) for v in matches_by_pl.values())
    log("Matches on active placements: %d" % total_active_matches)

    # ------------------------------------------------------------------
    # Per-placement computed structure
    # ------------------------------------------------------------------
    placement_info = {}
    for pid, f in active_placements.items():
        headline = f.get(F_PL_HEADLINE) or "(no headline)"
        cid = link_id(f.get(F_PL_CLIENT))
        csm_tags = f.get(F_PL_CSMTAGS) or []
        mrows = matches_by_pl.get(pid, [])

        # Check if placement is "New" (< 14 days old)
        created_date = parse_date(f.get("_createdTime"))
        days_since_created = (TODAY - created_date).days if created_date else 999
        is_new = days_since_created < 14

        viable = 0
        matched = approved = intro_after = rejected = 0
        ci_fresh = 0   # close-to-intro fresh <14d
        src_fresh = 0  # active-sourcing fresh <14d
        # New stage breakdown (exclude rejected-by-rating)
        sourcing = approved_int = introduced = interview = assessment = final = rating_rejected = 0
        sourcing_fresh = 0  # sourcing candidates < 14 days in status

        # For action tags calculation
        stalled_intro_3d = 0  # in introduced for 3+ days
        stalled_interview_assess_7d = 0  # in interview/assessment for 7+ days

        for mf in mrows:
            st = status_of(mf)
            rej = is_rejected(mf)
            d = days_of(mf)

            # viable (non-rejected + live pipeline)
            if not rej and st in VIABLE_STATUSES:
                viable += 1

            # client-health buckets (rating override -> rejected)
            if rej or st in CH_REJECTED_STATUSES:
                rejected += 1
            elif st in CH_APPROVED:
                approved += 1
            elif st in CH_INTRO_AFTER:
                intro_after += 1
            else:
                matched += 1

            # fresh pools for shortage (non-rejected, fresh <14d)
            if not rej and d is not None and d < 14:
                if st in CLOSE_TO_INTRO_STATUSES:
                    ci_fresh += 1
                elif st in ACTIVE_SOURCING_STATUSES:
                    src_fresh += 1

            # New stage breakdown (exclude rejected-by-rating and excluded statuses from stage counts)
            if rej or st in EXCLUDED_STATUSES:
                rating_rejected += 1
            elif st in CH_SOURCING:
                sourcing += 1
            elif st in CH_APPROVED_INT:
                approved_int += 1
            elif st in CH_INTRODUCED:
                introduced += 1
            elif st in CH_INTERVIEW:
                interview += 1
            elif st in CH_ASSESSMENT:
                assessment += 1
            elif st in CH_FINAL:
                final += 1

            # Action tags: count stalled candidates (exclude if 14+ days = lost)
            if not rej and d is not None:
                if st in CH_INTRODUCED and d >= 3 and d < 14:
                    stalled_intro_3d += 1
                elif st in (CH_INTERVIEW | CH_ASSESSMENT) and d >= 7 and d < 14:
                    stalled_interview_assess_7d += 1

            # Count active sourcing candidates (< 14 days in status)
            if not rej and st in CH_SOURCING and d is not None and d < 14:
                sourcing_fresh += 1

        # Pipeline health status (Healthy / Thin / Not healthy / New)
        # Order matters. A role already in trial or final selection is Healthy by
        # definition, the pipeline did its job. After that, later stages win
        # outright: a role deep in interviews is never dragged down by a thin
        # matching pool.
        pl_fulfilment = (f.get(F_PL_STATUS) or "").strip()
        in_late_role = pl_fulfilment in CLOSE_PLACEMENT_STATUSES

        later_stages_total = approved_int + introduced + interview + assessment + final
        if in_late_role:
            health_status = "Healthy"
        elif is_new:
            health_status = "New"
        elif later_stages_total >= 2:
            health_status = "Healthy"          # real depth past matching
        elif sourcing_fresh > 5:
            health_status = "Healthy"          # strong fresh matching pool
        elif sourcing_fresh <= 3:
            health_status = "Not healthy"      # fresh matching pool drying up
        elif later_stages_total == 1:
            health_status = "Thin"             # 1 past matching, 4-5 fresh behind
        else:
            health_status = "Not healthy"

        # Action tags (only if not "New").
        # "Needs matching" always shows alone, it means the rest is too low to act on.
        action_tags = []
        if in_late_role:
            action_tags.append("In final selection"
                               if pl_fulfilment == "90 - In Final Selection" else "In trial")
            if stalled_interview_assess_7d >= 1:
                action_tags.append("Needs push on interviews/assessments")
            if final >= 1:
                action_tags.append("Need to push for a hire")
        elif not is_new:
            if health_status in ("Not healthy", "Thin"):
                action_tags = ["Needs matching"]
            else:
                if stalled_intro_3d >= 2:
                    action_tags.append("Needs client feedback")
                if stalled_interview_assess_7d >= 1:
                    action_tags.append("Needs push on interviews/assessments")
                if final >= 1:
                    action_tags.append("Need to push for a hire")

        placement_info[pid] = {
            "headline": headline,
            "cid": cid,
            "csm_tags": csm_tags,
            "fulfilment": (f.get(F_PL_STATUS) or "").strip(),
            "is_new": is_new,
            "days_open": days_since_created if created_date else None,
            "viable": viable,
            "matched": matched,
            "approved": approved,
            "intro_after": intro_after,
            "rejected": rejected,
            "total": len(mrows),
            "ci_fresh": ci_fresh,
            "src_fresh": src_fresh,
            # New stage breakdown
            "sourcing": sourcing,
            "approved_int": approved_int,
            "introduced": introduced,
            "interview": interview,
            "assessment": assessment,
            "final": final,
            "rating_rejected": rating_rejected,
            # Pipeline health & action tags
            "health_status": health_status,
            "action_tags": action_tags,
        }

    # ------------------------------------------------------------------
    # A. Pipeline funnel (whole active book, rejected excluded)
    # ------------------------------------------------------------------
    funnel = {"Sourcing": 0, "Introduced": 0, "Interview": 0, "Assessment": 0,
              "Trial": 0, "Negotiation": 0, "Hired": 0}
    for pid in active_placements:
        for mf in matches_by_pl.get(pid, []):
            if is_rejected(mf):
                continue
            st = status_of(mf)
            if st in CH_REJECTED_STATUSES:
                continue  # dead statuses never shown
            if st in FUNNEL_INTRODUCED:
                funnel["Introduced"] += 1
            elif st in FUNNEL_INTERVIEW:
                funnel["Interview"] += 1
            elif st in FUNNEL_ASSESSMENT:
                funnel["Assessment"] += 1
            elif st in FUNNEL_TRIAL:
                funnel["Trial"] += 1
            elif st in FUNNEL_NEGOTIATION:
                funnel["Negotiation"] += 1
            elif st in FUNNEL_HIRED:
                funnel["Hired"] += 1
            else:
                funnel["Sourcing"] += 1

    # ------------------------------------------------------------------
    # Top strip
    # ------------------------------------------------------------------
    n_active_clients = len(clients_with_active)
    n_open_roles = len(active_placements)
    # candidates in play = intro-or-further, not rejected (= viable count across book)
    n_in_play = sum(placement_info[pid]["viable"] for pid in active_placements)

    # ------------------------------------------------------------------
    # Hires last 7 days (all closed-successful, any client status)
    # ------------------------------------------------------------------
    cutoff = TODAY.toordinal() - 7
    # need hired candidate name -> resolve from matches with status 17 - Hired on that placement
    hired_name_by_pl = {}
    for m in matches:
        f = m["fields"]
        if status_of(f) == "17 - Hired":
            pid = link_id(f.get(F_M_POSITION))
            if pid:
                hired_name_by_pl.setdefault(pid, name_of(f))

    hires = []
    churns = []
    for p in placements:
        f = p["fields"]
        st = f.get(F_PL_STATUS)
        headline = f.get(F_PL_HEADLINE) or "(no headline)"
        if st == "100 - Closed | Successful":
            cd = parse_date(f.get(F_PL_CLOSED_TS)) or parse_date(f.get(F_PL_CLOSED_FALLBACK))
            if cd and cd.toordinal() >= cutoff:
                hires.append((headline, hired_name_by_pl.get(p["id"], ""), cd))
        elif st == "-20 - Churned":
            cd = parse_date(f.get(F_PL_CHURN_DATE)) or parse_date(f.get(F_PL_CLOSED_TS))
            if cd and cd.toordinal() >= cutoff:
                churns.append((headline, cd))
    hires.sort(key=lambda x: x[2], reverse=True)
    churns.sort(key=lambda x: x[1], reverse=True)

    # ------------------------------------------------------------------
    # B. Client health (emptiest first)
    # ------------------------------------------------------------------
    clients_health = {}  # cid -> {name, placements:[pid], total_viable, uncovered}
    for pid in active_placements:
        cid = placement_info[pid]["cid"]
        clients_health.setdefault(cid, {"pids": []})
        clients_health[cid]["pids"].append(pid)

    ch_rows = []
    n_uncovered_clients = 0
    for cid, info in clients_health.items():
        pids = info["pids"]
        total_viable = sum(placement_info[p]["viable"] for p in pids)
        uncovered = any(placement_info[p]["viable"] < 3 for p in pids)
        n_uncovered_roles = sum(1 for p in pids if placement_info[p]["viable"] < 3)
        if uncovered:
            n_uncovered_clients += 1
        ch_rows.append({
            "cid": cid,
            "name": client_name.get(cid, "(unnamed)"),
            "pids": pids,
            "total_viable": total_viable,
            "n_roles": len(pids),
            "uncovered": uncovered,
            "n_uncovered_roles": n_uncovered_roles,
        })
    # sort: total_viable asc, then more uncovered roles first, then name A-Z
    ch_rows.sort(key=lambda r: (r["total_viable"], -r["n_uncovered_roles"], r["name"].lower()))

    # ------------------------------------------------------------------
    # B2/B3. Interview + Assessment tabs (grouped by stage)
    # ------------------------------------------------------------------
    def stage_rows(status_value):
        rows = []
        for pid in active_placements:
            head = placement_info[pid]["headline"]
            for mf in matches_by_pl.get(pid, []):
                if is_rejected(mf):
                    continue
                if status_of(mf) == status_value:
                    d = days_of(mf)
                    rows.append((name_of(mf), head, d))
        rows.sort(key=lambda r: (-(r[2] if r[2] is not None else -1), r[0]))
        return rows

    interview_sections = [(label, cls, stage_rows(st)) for (label, cls, st) in INTERVIEW_SECTIONS]
    assessment_sections = [(label, cls, stage_rows(st)) for (label, cls, st) in ASSESSMENT_SECTIONS]
    n_interview = sum(len(s[2]) for s in interview_sections)
    n_assessment = sum(len(s[2]) for s in assessment_sections)

    # ------------------------------------------------------------------
    # C. Candidate shortage (tiers)
    # ------------------------------------------------------------------
    short = []
    for pid in active_placements:
        pi = placement_info[pid]
        if pi["viable"] < 3:
            short.append(pi)

    def csm_label(tags):
        if not tags:
            return None
        return ", ".join(tags)

    # Roles opened under 14 days ago are too new to judge. They are pulled out
    # BEFORE tiering, so they can never sit in Tier 1 Truly empty and therefore
    # can never be picked up by the CSM Tags write on a manual approved run.
    tier_new = [pi for pi in short if pi.get("is_new")]
    judged = [pi for pi in short if not pi.get("is_new")]

    tier1, tier2, tier3, tier4 = [], [], [], []
    for pi in judged:
        v, ci, src = pi["viable"], pi["ci_fresh"], pi["src_fresh"]
        if v >= 1:
            tier4.append(pi)
        elif ci >= 1:
            tier3.append(pi)
        elif src >= 1:
            tier2.append(pi)
        else:
            tier1.append(pi)
    for t in (tier1, tier2, tier3, tier4):
        t.sort(key=lambda pi: (pi["viable"], pi["headline"].lower()))
    # newest first, they are the least actionable
    tier_new.sort(key=lambda pi: (pi["days_open"] if pi["days_open"] is not None else 99,
                                  pi["headline"].lower()))
    n_short = len(judged)
    n_short_new = len(tier_new)

    # ------------------------------------------------------------------
    # D. Close to hire (late candidate stages only)
    # ------------------------------------------------------------------
    # Driven by the ROLE: every placement sitting at 90 - In Final Selection or
    # 70 - In Trial shows up, whether or not it has a late-stage candidate.
    # Under each role, only candidates past 'interview completed'.
    close_groups = []   # [(placement_status_label, [ (headline, [cand rows]) ]) ]
    n_close = 0
    n_close_roles = 0
    for pl_status in CLOSE_PLACEMENT_STATUSES:
        roles = []
        for pid, pi in placement_info.items():
            if pi["fulfilment"] != pl_status:
                continue
            cands = []
            for mf in matches_by_pl.get(pid, []):
                if is_rejected(mf):
                    continue
                st = status_of(mf)
                if st in PAST_INTERVIEW_STATUSES:
                    cands.append((name_of(mf), st, days_of(mf)))
            cands.sort(key=lambda x: (-(x[2] if x[2] is not None else -1), x[0]))
            roles.append((pi["headline"], cands))
            n_close += len(cands)
            n_close_roles += 1
        # roles with nobody past interview float to the top, they are the risk
        roles.sort(key=lambda x: (len(x[1]), x[0].lower()))
        close_groups.append((pl_status, roles))

    # ------------------------------------------------------------------
    # Build HTML
    # ------------------------------------------------------------------
    computed = {
        "n_active_clients": n_active_clients,
        "n_open_roles": n_open_roles,
        "n_in_play": n_in_play,
        "n_hires": len(hires),
        "funnel": funnel,
        "n_uncovered_clients": n_uncovered_clients,
        "n_short": n_short,
        "tiers": (len(tier1), len(tier2), len(tier3), len(tier4)),
        "n_interview": n_interview,
        "n_assessment": n_assessment,
        "n_close": n_close,
    }

    html = build_html(
        n_active_clients, n_open_roles, n_in_play, hires, churns, funnel,
        ch_rows, placement_info, n_uncovered_clients,
        interview_sections, assessment_sections, n_interview, n_assessment,
        tier1, tier2, tier3, tier4, n_short, tier_new, n_short_new,
        close_groups, n_close, n_close_roles,
    )

    # sanity: no em/en dashes
    if "—" in html or "–" in html:
        raise RuntimeError("HTML contains an em/en dash, aborting before publish")

    # save local copy (vault archive). Non-fatal: the cloud routine has no vault.
    try:
        os.makedirs(DASHBOARDS_DIR, exist_ok=True)
        local_path = os.path.join(DASHBOARDS_DIR, "(C) %s dashboard.html" % TODAY.isoformat())
        with open(local_path, "w", encoding="utf-8") as fh:
            fh.write(html)
        log("Local dashboard written: %s" % local_path)
    except Exception as e:
        log("Local copy skipped (%s), continuing to publish" % e.__class__.__name__)

    # publish to GitHub
    sha = publish_github(cfg["github_pat"], cfg["repo"], html)
    log("Published to GitHub. Commit sha: %s" % sha)

    log("SUCCESS  active_clients=%d open_roles=%d in_play=%d hires=%d "
        "funnel=%s tiers=%s short=%d interview=%d assessment=%d close=%d sha=%s" % (
            n_active_clients, n_open_roles, n_in_play, len(hires),
            json.dumps(funnel), str(computed["tiers"]), n_short,
            n_interview, n_assessment, n_close, sha))

    # print a compact machine-readable summary for the caller
    print("RESULT " + json.dumps({**computed, "sha": sha, "path": os.path.abspath(__file__)}))
    return 0


# ---------------------------------------------------------------------------
# HTML builder (matches locked template)
# ---------------------------------------------------------------------------
def build_html(n_active_clients, n_open_roles, n_in_play, hires, churns, funnel,
               ch_rows, pi_map, n_uncovered_clients,
               interview_sections, assessment_sections, n_interview, n_assessment,
               tier1, tier2, tier3, tier4, n_short, tier_new, n_short_new,
               close_groups, n_close, n_close_roles):

    today_pretty = pretty_date(TODAY)
    B = "&middot;"

    # --- funnel bars ---
    fmax = max(funnel.values()) or 1
    funnel_order = [
        ("Sourcing", "#94a3b8"),
        ("Introduced", "#3b82f6"),
        ("Interview", "#06b6d4"),
        ("Assessment", "#8b5cf6"),
        ("Trial", "#f59e0b"),
        ("Negotiation", "#ec4899"),
        ("Hired", "#22c55e"),
    ]
    frows = []
    for label, color in funnel_order:
        n = funnel[label]
        pct = max(3, round(n / fmax * 100)) if n else 3
        frows.append(
            '<div class="frow">\n'
            '      <div class="flabel">%s</div>\n'
            '      <div class="ftrack"><div class="fbar" style="width:%d%%;background:%s"></div></div>\n'
            '      <div class="fval">%d</div>\n'
            '    </div>' % (label, pct, color, n))
    funnel_html = "".join(frows)

    # --- hires block ---
    if hires:
        items = []
        for head, cand, cd in hires:
            if cand:
                items.append('<li><b>%s</b> <span class="muted">%s %s %s closed %s</span></li>'
                             % (esc(cand), B, esc(head), B, pretty_date(cd)))
            else:
                items.append('<li><b>%s</b> <span class="muted">%s closed %s</span></li>'
                             % (esc(head), B, pretty_date(cd)))
        hires_list = "<ul class=\"winlist\">%s</ul>" % "".join(items)
    else:
        hires_list = '<div class="muted small">No roles filled in the last 7 days</div>'

    if churns:
        items = []
        for head, cd in churns:
            items.append('<li>%s <span class="muted">%s cancelled %s</span></li>'
                         % (esc(head), B, pretty_date(cd)))
        churn_list = "<ul class=\"losslist\">%s</ul>" % "".join(items)
    else:
        churn_list = '<div class="muted small">No roles cancelled in the last 7 days</div>'

    # --- client health (flat list of placements) ---
    ch_html_parts = []
    # Flat list: all placements sorted by client name, then by viable count
    all_placements = []
    for r in ch_rows:
        for pid in r["pids"]:
            pi = pi_map[pid]
            all_placements.append((r["name"], pid, pi))

    # Sort: Not healthy first, then Thin, then Healthy, then New; within each group sort by client name, then by viable count
    health_order = {"Not healthy": 0, "Thin": 1, "Healthy": 2, "New": 3}
    all_placements.sort(key=lambda x: (
        health_order.get(x[2]["health_status"], 3),
        x[0].lower(),
        -(x[2]["viable"] if x[2]["viable"] is not None else -1)
    ))

    # Calculate KPIs
    n_clients = len(ch_rows)
    n_placements = len(all_placements)
    avg_placements = round(n_placements / n_clients, 1) if n_clients > 0 else 0

    # Count by health status
    health_counts = {"Not healthy": 0, "Thin": 0, "Healthy": 0, "New": 0}
    for _, _, pi in all_placements:
        status = pi["health_status"]
        if status in health_counts:
            health_counts[status] += 1

    # Calculate percentages (as integers)
    not_healthy_pct = int(round(health_counts["Not healthy"] / n_placements * 100)) if n_placements > 0 else 0
    thin_pct = int(round(health_counts["Thin"] / n_placements * 100)) if n_placements > 0 else 0
    healthy_pct = int(round(health_counts["Healthy"] / n_placements * 100)) if n_placements > 0 else 0
    new_pct = int(round(health_counts["New"] / n_placements * 100)) if n_placements > 0 else 0

    # Build KPI section
    kpi_html = (
        '<div class="kpi-section">\n'
        '  <div class="kpi-cards">\n'
        '    <div class="kpi-card"><div class="kpi-num">%d</div><div class="kpi-label">Clients</div></div>\n'
        '    <div class="kpi-card"><div class="kpi-num">%d</div><div class="kpi-label">Placements</div></div>\n'
        '    <div class="kpi-card"><div class="kpi-num">%.1f</div><div class="kpi-label">Avg per client</div></div>\n'
        '  </div>\n'
        '  <div class="kpi-breakdown">\n'
        '    <div class="kpi-bar">\n'
        '      <div class="kpi-segment" style="width:%d%%;background:#fee2e2;"></div>\n'
        '      <div class="kpi-segment" style="width:%d%%;background:#fed7aa;"></div>\n'
        '      <div class="kpi-segment" style="width:%d%%;background:#dcfce7;"></div>\n'
        '      <div class="kpi-segment" style="width:%d%%;background:#dbeafe;"></div>\n'
        '    </div>\n'
        '    <div class="kpi-legend">\n'
        '      <div><span class="leg-dot" style="background:#fee2e2"></span>Not Healthy %d%%</div>\n'
        '      <div><span class="leg-dot" style="background:#fed7aa"></span>Thin %d%%</div>\n'
        '      <div><span class="leg-dot" style="background:#dcfce7"></span>Healthy %d%%</div>\n'
        '      <div><span class="leg-dot" style="background:#dbeafe"></span>New %d%%</div>\n'
        '    </div>\n'
        '  </div>\n'
        '</div>\n'
    ) % (
        n_clients, n_placements, avg_placements,
        not_healthy_pct, thin_pct, healthy_pct, new_pct,
        not_healthy_pct, thin_pct, healthy_pct, new_pct
    )

    ch_html_parts.append(kpi_html)

    legend_html = (
        '<div class="status-legend">\n'
        '  <div class="legend-block">\n'
        '    <div class="legend-title">Role health, how deep the pipeline is</div>\n'
        '    <div class="legend-item"><span class="chip red">Not healthy</span> under 2 candidates past matching, and 3 or fewer fresh people left in matching, so there is nothing coming and nothing to send</div>\n'
        '    <div class="legend-item"><span class="chip orange">Thin</span> exactly 1 candidate past matching, with only 4 or 5 fresh people behind them, so one drop-off leaves it empty</div>\n'
        '    <div class="legend-item"><span class="chip green">Healthy</span> the role is in final selection or trial, or it has 2 or more candidates past matching, or more than 5 fresh people still in matching. Depth always wins, a role in interviews is never marked down for a thin top of funnel</div>\n'
        '    <div class="legend-item"><span class="chip blue">New</span> role opened less than 2 weeks ago, too early to judge, no action needed yet</div>\n'
        '  </div>\n'
        '  <div class="legend-block">\n'
        '    <div class="legend-title">What the role needs next</div>\n'
        '    <div class="legend-item"><span class="chip stage-late">In final selection</span><span class="chip stage-late">In trial</span> the role itself is at that fulfilment status in Airtable. Always counts as Healthy whatever the candidate numbers say, the pipeline already did its job</div>\n'
        '    <div class="legend-item"><span class="chip action-low">Needs matching</span> the role is Not healthy or Thin, so the team has to find and put people forward. Always shows on its own, the rest is too low to act on yet</div>\n'
        '    <div class="legend-item"><span class="legend-note-inline">Fresh</span> means the candidate changed status within the last 2 weeks. A stale pool does not count towards health</div>\n'
        '    <div class="legend-item"><span class="chip action-med">Needs client feedback</span> 2 or more people sitting introduced for 3+ days with no answer from the client</div>\n'
        '    <div class="legend-item"><span class="chip action-high">Needs push on interviews/assessments</span> someone has been stuck in an interview or assessment step for 7+ days</div>\n'
        '    <div class="legend-item"><span class="chip action-urgent">Need to push for a hire</span> someone is in trial or negotiation, closest to signing, keep it moving</div>\n'
        '  </div>\n'
        '  <div class="legend-note">Candidates rated Rejected, plus offer ghosted and offer rejected, are left out of every stage count and only show under Rejected</div>\n'
        '</div>\n'
    )
    ch_html_parts.append(legend_html)

    for client_name, pid, pi in all_placements:
        # Health status badge color
        health_color = {"Healthy": "green", "Thin": "orange", "Not healthy": "red", "New": "blue"}.get(pi["health_status"], "gray")
        health_badge = '<span class="chip %s">%s</span>' % (health_color, pi["health_status"])

        # Action tags with urgency-based colors (closer to hire = stronger color)
        action_color_map = {
            "In final selection": "stage-late",
            "In trial": "stage-late",
            "Needs matching": "action-low",
            "Needs client feedback": "action-med",
            "Needs push on interviews/assessments": "action-high",
            "Need to push for a hire": "action-urgent"
        }
        action_badges = ""
        for tag in pi["action_tags"]:
            tag_color = action_color_map.get(tag, "orange")
            action_badges += '<span class="chip %s">%s</span>' % (tag_color, tag)

        ch_html_parts.append(
            '<div class="plrow">\n'
            '          <div class="plname">%s %s</div>\n'
            '          <div class="plstatus">%s</div>\n'
            '          <div class="plbreak"><span class="chip live">%d Matching</span>\n'
            '            <span class="chip">%d Approved Int</span>\n'
            '            <span class="chip">%d Introduced</span>\n'
            '            <span class="chip">%d Interview</span>\n'
            '            <span class="chip">%d Assessment</span>\n'
            '            <span class="chip">%d Final</span>\n'
            '            <span class="chip grey">%d Rejected</span></div>\n'
            '        </div>' % (esc(pi["headline"]), health_badge, action_badges, pi["sourcing"],
                                pi["approved_int"], pi["introduced"], pi["interview"],
                                pi["assessment"], pi["final"], pi["rating_rejected"]))
    ch_html = "".join(ch_html_parts)

    # --- interview / assessment sections ---
    def render_stage_sections(sections):
        out = []
        for label, cls, rows in sections:
            row_html = []
            for cand, role, d in rows:
                dot = dot_for_days(d)
                dstr = ("%dd" % d) if d is not None else ""
                row_html.append(
                    '<div class="stgrow"><span class="sdot">%s</span>'
                    '<span class="sname">%s</span>'
                    '<span class="srole">%s</span>'
                    '<span class="sdays">%s</span></div>'
                    % (dot, esc(cand), esc(role), dstr))
            out.append(
                '<div class="ivstage %s"><div class="ivhead">%s <span class="cnt">(%d)</span></div>%s</div>'
                % (cls, label, len(rows), "".join(row_html)))
        return "".join(out)

    # Group by status, sort by days within each group (longest-waiting first)
    interview_html = ""
    for label, cls, rows in interview_sections:
        sorted_rows = sorted(rows, key=lambda x: (-(x[2] if x[2] is not None else -1), x[0]))
        row_html = []
        for cand, role, d in sorted_rows:
            dot = dot_for_days(d)
            dstr = ("%dd" % d) if d is not None else ""
            row_html.append(
                '<div class="stgrow"><span class="sdot">%s</span>'
                '<span class="sname">%s</span>'
                '<span class="srole">%s</span>'
                '<span class="sdays">%s</span></div>'
                % (dot, esc(cand), esc(role), dstr))
        interview_html += (
            '<div class="ivstage %s"><div class="ivhead">%s <span class="cnt">(%d)</span></div>%s</div>'
            % (cls, label, len(rows), "".join(row_html)))

    assessment_html = render_stage_sections(assessment_sections)

    # --- shortage tiers ---
    def age_cell(pi):
        d = pi.get("days_open")
        if d is None:
            return '<span class="muted">?</span>'
        return "%dd" % d

    def render_tier(name, cls, desc, rows):
        body = []
        for pi in rows:
            tag = ", ".join(pi["csm_tags"]) if pi["csm_tags"] else None
            csm_cell = esc(tag) if tag else '<span class="muted">no tag</span>'
            body.append(
                '<tr><td class="num">%d</td><td class="num">%d</td><td class="num">%d</td>\n'
                '          <td class="num age">%s</td><td>%s</td><td class="csm">%s</td></tr>'
                % (pi["viable"], pi["ci_fresh"], pi["src_fresh"], age_cell(pi),
                   esc(pi["headline"]), csm_cell))
        return (
            '<div class="tier %s">\n'
            '      <div class="tierhead"><span class="tiername">%s</span> <span class="tiercount">%d</span>\n'
            '        <div class="tierdesc">%s</div></div>\n'
            '      <table class="stbl"><thead><tr><th>In play</th><th>Close to intro</th>'
            '<th>Sourcing</th><th>Open</th><th>Role</th><th>Owner</th></tr></thead>\n'
            '      <tbody>%s</tbody></table></div>'
            % (cls, name, len(rows), desc, "".join(body)))

    shortage_html = ""
    if tier1:
        shortage_html += render_tier("Truly empty", "red",
                                     "Nothing anywhere, source these from scratch now", tier1)
    if tier2:
        shortage_html += render_tier("Sourcing in motion", "amber",
                                     "Being worked, still early, nobody near intro yet", tier2)
    if tier3:
        shortage_html += render_tier("Intro incoming", "blue",
                                     "Someone is about to be introduced, lowest sourcing priority", tier3)
    if tier4:
        shortage_html += render_tier("Some in play", "green",
                                     "1 or 2 already live, partly covered but still under three", tier4)
    if tier_new:
        shortage_html += render_tier(
            "Too new to judge", "grey",
            "Opened under 2 weeks ago, so a thin pipeline is expected. Not counted in the "
            "shortage number above and never eligible for a Need More Sourcing tag", tier_new)

    # --- close to hire ---
    CLOSE_HINTS = {
        "90 - In Final Selection": "Client is picking their person, closest to a signature",
        "70 - In Trial": "Someone is on a paid trial for this role right now",
    }

    def render_close_group(pl_status, roles):
        if not roles:
            return ""
        blocks = []
        for head, cands in roles:
            if cands:
                lines = []
                for cand, st, d in cands:
                    dot_cls = ""
                    dot = ""
                    if d is not None and d > 14:
                        dot_cls = ' red'
                        dot = "\U0001f534"
                    elif d is not None and d > 7:
                        dot_cls = ' amber'
                        dot = "\U0001f7e0"
                    title_attr = (' title="14+ days in stage"' if dot == "\U0001f534"
                                  else (' title="7+ days in stage"' if dot == "\U0001f7e0" else ""))
                    dstr = ("%dd" % d) if d is not None else ""
                    lines.append(
                        '<div class="candline"><span class="dot%s"%s>%s</span>'
                        '<span class="cn">%s</span>'
                        '<span class="cst">%s</span>'
                        '<span class="cd">%s</span></div>'
                        % (dot_cls, title_attr, dot, esc(cand), st_pretty(st), dstr))
                body = "".join(lines)
                count_chip = '<span class="cnt">(%d past interview)</span>' % len(cands)
            else:
                body = ('<div class="candline empty">Nobody past interview on this role yet, '
                        'the status says late stage but the pipeline does not back it up</div>')
                count_chip = '<span class="chip red">0 past interview</span>'
            blocks.append(
                '<div class="ctrole"><div class="ctrolehead">%s %s</div>%s</div>'
                % (esc(head), count_chip, body))
        return (
            '<div class="ctsec"><div class="cthead">%s <span class="cnt">(%d roles)</span> '
            '<span class="cthd">%s</span></div>%s</div>'
            % (esc(pl_status), len(roles), CLOSE_HINTS.get(pl_status, ""), "".join(blocks)))

    close_html = ""
    for pl_status, roles in close_groups:
        close_html += render_close_group(pl_status, roles)
    if not close_html:
        close_html = '<div class="muted small">No roles sitting in final selection or trial right now</div>'

    # captions
    health_cap = ("%d active clients with an open role, %d have at least one role under 3 in play. "
                  "Emptiest first. Open a client to see each role and how its candidates break down"
                  % (n_active_clients, n_uncovered_clients))

    doc = TEMPLATE % {
        "title_date": today_pretty,
        "n_active_clients": n_active_clients,
        "n_open_roles": n_open_roles,
        "n_in_play": n_in_play,
        "n_hires": len(hires),
        "funnel_rows": funnel_html,
        "hires_list": hires_list,
        "churn_list": churn_list,
        "health_cap": health_cap,
        "client_health": ch_html,
        "interview_cap_n": n_interview,
        "interview_body": interview_html,
        "assessment_cap_n": n_assessment,
        "assessment_body": assessment_html,
        "shortage_n": n_short,
        "shortage_new": n_short_new,
        "shortage_body": shortage_html,
        "close_n": n_close,
        "close_roles": n_close_roles,
        "close_body": close_html,
    }
    return doc


def st_pretty(st):
    m = {
        "12 - Negotiation": "Negotiation",
        "13 - Trial To Start": "Trial to start",
        "13 - Trial In Progress": "Trial in progress",
        "14 - Trial Completed": "Trial completed",
        "11 - Interview - Completed": "Interview completed",
        "13 - Assessment To Send": "Assessment to send",
        "14 - Assessment Shared": "Assessment shared",
        "15 - Assessment In Progress": "Assessment in progress",
        "16 - Assessment Completed": "Assessment completed",
        "17 - Hired": "Hired",
    }
    return m.get((st or "").strip(), st)


# ---------------------------------------------------------------------------
# GitHub publish (the ONLY write)
# ---------------------------------------------------------------------------
def publish_github(pat, repo, html):
    api = "https://api.github.com/repos/%s/contents/index.html" % repo
    headers = {
        "Authorization": "Bearer " + pat,
        "Accept": "application/vnd.github+json",
        "User-Agent": "hiry-metrics-refresh",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # get current sha (may 404 if file doesn't exist yet)
    sha = None
    req = urllib.request.Request(api + "?ref=main", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            sha = json.load(r).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise

    content_b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
    payload = {
        "message": "Dashboard %s" % TODAY.isoformat(),
        "content": content_b64,
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(api, data=data, headers=headers, method="PUT")
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.load(r)
    return resp["commit"]["sha"]


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------
TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Client Metrics &middot; %(title_date)s</title>
<style>
:root{--bg:#f5f6f8;--card:#ffffff;--border:#e5e7eb;--ink:#1f2430;--muted:#6b7280;--shadow:0 1px 3px rgba(16,24,40,.06),0 1px 2px rgba(16,24,40,.04);}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5}
.wrap{max-width:1080px;margin:0 auto;padding:28px 22px 60px}
h1{font-size:22px;margin:0 0 2px;font-weight:700}
.sub{color:var(--muted);font-size:13px;margin-bottom:22px}
.strip{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:22px}
.stat{background:var(--card);border:1px solid var(--border);border-radius:12px;box-shadow:var(--shadow);padding:18px 18px 16px}
.stat .n{font-size:30px;font-weight:700;letter-spacing:-.5px}
.stat .l{color:var(--muted);font-size:12.5px;margin-top:3px}
.stat .sub{color:var(--muted);font-size:11px;margin-top:7px;line-height:1.35;opacity:.85}
.tabs{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px}
.tab{border:none;cursor:pointer;font:inherit;font-weight:600;font-size:13px;padding:9px 15px;border-radius:999px;background:#eceef1;color:#4b5563;transition:.12s}
.tab:hover{background:#e2e5ea}
.tab.active{color:#fff}
.tab[data-t="funnel"].active{background:#3b82f6}
.tab[data-t="health"].active{background:#0ea5e9}
.tab[data-t="interview"].active{background:#06b6d4}
.tab[data-t="assessment"].active{background:#8b5cf6}
.tab[data-t="shortage"].active{background:#f59e0b}
.tab[data-t="close"].active{background:#22c55e}
.panel{display:none;background:var(--card);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow);padding:22px}
.panel.active{display:block}
.phead{font-size:16px;font-weight:700;margin:0 0 3px}
.pcap{color:var(--muted);font-size:13px;margin-bottom:18px}
.muted{color:var(--muted)} .small{font-size:12.5px}
.frow{display:grid;grid-template-columns:120px 1fr 44px;align-items:center;gap:12px;margin:9px 0}
.flabel{font-weight:600;font-size:13px}
.ftrack{background:#f1f3f5;border-radius:8px;height:26px;overflow:hidden}
.fbar{height:100%%;border-radius:8px;transition:width .3s}
.fval{font-weight:700;text-align:right}
.blocks{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:26px}
.block{border:1px solid var(--border);border-radius:12px;padding:16px}
.block h3{margin:0 0 10px;font-size:14px}
.winlist,.losslist{list-style:none;padding:0;margin:0}
.winlist li,.losslist li{padding:6px 0;border-bottom:1px solid #f0f1f3;font-size:13px}
.winlist li:last-child,.losslist li:last-child{border-bottom:none}
.block.win h3{color:#16a34a} .block.loss h3{color:#dc2626}
.client{border:1px solid var(--border);border-radius:11px;margin-bottom:9px;overflow:hidden;background:#fff}
.client summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:12px;padding:13px 15px}
.client summary::-webkit-details-marker{display:none}
.cname{font-weight:600;flex:0 0 auto;min-width:150px}
.csummary{color:var(--muted);font-size:12.5px;flex:1}
.pill{font-size:11.5px;font-weight:600;padding:3px 9px;border-radius:999px}
.pill.red{background:#fee2e2;color:#b91c1c} .pill.green{background:#dcfce7;color:#15803d}
.plwrap{padding:2px 15px 13px;border-top:1px solid #f0f1f3}
.plrow{padding:9px 0;border-bottom:1px solid #f0f1f3;margin-bottom:8px} .plrow:last-child{border-bottom:none}
.plname{font-weight:600;font-size:13px;margin-bottom:5px}
.client-label{color:#6b7280;font-weight:400;font-size:12px}
.mini.red{background:#fee2e2;color:#b91c1c;font-size:10.5px;font-weight:600;padding:1px 6px;border-radius:5px}
.chip{display:inline-block;font-size:11.5px;background:#f1f3f5;color:#374151;padding:3px 8px;border-radius:6px;margin:2px 4px 2px 0}
.chip.live{background:#e0f2fe;color:#075985;font-weight:600}
.chip.grey{background:#f3f4f6;color:#9ca3af} .chip.tot{background:#eef2ff;color:#4338ca}
.chip.green{background:#dcfce7;color:#15803d;font-weight:600} .chip.red{background:#fecaca;color:#991b1b;font-weight:600} .chip.blue{background:#dbeafe;color:#1e40af;font-weight:600} .chip.orange{background:#fed7aa;color:#b45309;font-weight:600}
.chip.action-low{background:#fef08a;color:#854d0e;font-weight:600} .chip.action-med{background:#fed7aa;color:#92400e;font-weight:600} .chip.action-high{background:#fdba74;color:#9a3412;font-weight:600} .chip.action-urgent{background:#fb7185;color:#831843;font-weight:600}
.chip.stage-late{background:#ede9fe;color:#5b21b6;font-weight:700}
.plstatus{margin-bottom:6px;padding-top:2px}
.status-legend{background:#f9fafb;border:1px solid var(--border);border-radius:12px;padding:16px 18px;margin-bottom:22px}
.legend-block{margin-bottom:14px}
.legend-block:last-of-type{margin-bottom:8px}
.legend-title{font-weight:700;font-size:12.5px;margin-bottom:8px;color:#1f2430}
.legend-item{display:flex;align-items:baseline;gap:8px;font-size:12.5px;color:#4b5563;padding:3px 0;line-height:1.45}
.legend-item .chip{flex:0 0 auto;margin:0}
.legend-note{font-size:12px;color:#6b7280;border-top:1px solid #e5e7eb;padding-top:10px}
.legend-note-inline{flex:0 0 auto;font-size:11.5px;font-weight:700;color:#6b7280;background:#eceef1;padding:3px 8px;border-radius:6px}
.kpi-section{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:24px;padding:16px;background:#f9fafb;border-radius:12px;border:1px solid var(--border)}
.kpi-cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.kpi-card{text-align:center;padding:12px;background:#fff;border-radius:8px;border:1px solid #e5e7eb}
.kpi-num{font-size:24px;font-weight:700;color:#1f2430}
.kpi-label{font-size:11px;color:#6b7280;margin-top:4px;font-weight:500}
.kpi-breakdown{display:flex;flex-direction:column;gap:12px}
.kpi-bar{display:flex;height:24px;border-radius:6px;overflow:hidden;border:1px solid #e5e7eb}
.kpi-segment{flex:1}
.kpi-legend{display:flex;flex-direction:column;gap:6px;font-size:12px}
.leg-dot{display:inline-block;width:8px;height:8px;border-radius:50%%;margin-right:6px}
.ivrole{border:1px solid var(--border);border-radius:11px;padding:12px 14px;margin-bottom:10px}
.ivhead{font-weight:700;font-size:13.5px;margin-bottom:6px}
.cnt{color:var(--muted);font-weight:600}
.candline{display:grid;grid-template-columns:22px minmax(0,1fr) auto 48px;align-items:center;gap:8px;padding:5px 0;border-top:1px solid #f4f5f7;font-size:13px}
.candline:first-of-type{border-top:none}
.dot{width:22px;text-align:center;font-size:11px}
.cn{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis} .cst{color:var(--muted);font-size:12.5px;text-align:right;white-space:nowrap} .cd{text-align:right;color:#4b5563;font-variant-numeric:tabular-nums}
.dot,.sdot{display:inline-block;width:22px;text-align:center}
.cn,.sname,.cname,.plname,.srole,.cst{white-space:nowrap}
.cn,.sname{overflow:hidden;text-overflow:ellipsis}
.all-candidates{padding:0;margin:0}
.ivstage{border:1px solid var(--border);border-radius:11px;padding:12px 14px;margin-bottom:12px}
.ivstage.done{border-left:4px solid #06b6d4}
.ivstage.sched{border-left:4px solid #38bdf8}
.ivstage.tosched{border-left:4px solid #cbd5e1}
.stgrow{display:grid;grid-template-columns:22px minmax(0,1fr) minmax(0,1.1fr) 60px;align-items:center;gap:8px;padding:3px 0;border-top:1px solid #f4f5f7;font-size:12.5px}
.stgrow:first-of-type{border-top:none}
.sdot{width:22px;text-align:center;font-size:11px}
.sname{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.srole{color:var(--muted);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sdays{text-align:right;color:#4b5563;font-variant-numeric:tabular-nums}
.stage-label{display:inline-block;background:#f1f3f5;color:#4b5563;font-size:11px;font-weight:600;padding:2px 6px;border-radius:4px;margin-right:4px}
.tier{border:1px solid var(--border);border-left-width:4px;border-radius:11px;padding:14px 16px;margin-bottom:14px}
.tier.red{border-left-color:#ef4444} .tier.amber{border-left-color:#f59e0b} .tier.blue{border-left-color:#3b82f6} .tier.green{border-left-color:#22c55e}
.tier.grey{border-left-color:#cbd5e1;background:#fafafa;opacity:.72}
.tier.grey .tiername,.tier.grey .stbl td{color:#6b7280}
.stbl td.age{width:56px;color:#6b7280;font-size:12px}
.tierhead{margin-bottom:10px}
.tiername{font-weight:700;font-size:14px}
.tiercount{display:inline-block;background:#f1f3f5;color:#4b5563;font-weight:700;font-size:12px;padding:1px 8px;border-radius:999px;margin-left:6px}
.tierdesc{color:var(--muted);font-size:12.5px;margin-top:2px}
.stbl{width:100%%;border-collapse:collapse;font-size:13px}
.stbl th{text-align:left;color:var(--muted);font-size:11.5px;font-weight:600;padding:5px 8px;border-bottom:1px solid var(--border)}
.stbl td{padding:6px 8px;border-bottom:1px solid #f4f5f7}
.stbl td.num{text-align:center;font-variant-numeric:tabular-nums;width:70px;color:#4b5563}
.stbl td.csm{color:#4b5563;font-size:12.5px}
.stbl tr:last-child td{border-bottom:none}
.ctsec{margin-bottom:20px}
.cthead{font-weight:700;font-size:14px;margin-bottom:8px}
.cthd{color:var(--muted);font-weight:400;font-size:12.5px;margin-left:6px}
.ctrole{border:1px solid var(--border);border-radius:11px;padding:10px 14px;margin-bottom:9px}
.ctrolehead{font-weight:600;font-size:13px;margin-bottom:4px}
.ctrolehead .cnt{font-weight:600;margin-left:4px}
.candline.empty{display:block;color:#b91c1c;font-size:12.5px;padding:4px 0}
</style></head>
<body><div class="wrap">
<h1>Client Metrics</h1>
<div class="sub">Live snapshot &middot; %(title_date)s &middot; active clients only, candidates counted on roles we are actively working</div>

<div class="strip">
  <div class="stat"><div class="n">%(n_active_clients)d</div><div class="l">Active clients</div></div>
  <div class="stat"><div class="n">%(n_open_roles)d</div><div class="l">Open roles</div></div>
  <div class="stat"><div class="n">%(n_in_play)d</div><div class="l">Candidates in play</div><div class="sub">Introduced to the client or further along (interview, assessment, trial, negotiation). Excludes people still being sourced and anyone rejected.</div></div>
  <div class="stat"><div class="n">%(n_hires)d</div><div class="l">Hires last 7 days</div></div>
</div>

<div class="tabs">
  <button class="tab active" data-t="funnel">Pipeline funnel</button>
  <button class="tab" data-t="health">Client health</button>
  <button class="tab" data-t="interview">Interview</button>
  <button class="tab" data-t="assessment">Assessment</button>
  <button class="tab" data-t="shortage">Candidate shortage</button>
  <button class="tab" data-t="close">Close to hire</button>
</div>

<div class="panel active" id="funnel">
  <div class="phead">Pipeline funnel</div>
  <div class="pcap">Where every live candidate sits across the hiring stages. Rejected and dead candidates are left out entirely</div>
  %(funnel_rows)s
  <div class="blocks">
    <div class="block win"><h3>Hires &middot; last 7 days</h3>
      <div class="muted small" style="margin:-4px 0 8px">Roles we successfully filled in the last 7 days</div>
      %(hires_list)s</div>
    <div class="block loss"><h3>Churned &middot; last 7 days</h3>
      <div class="muted small" style="margin:-4px 0 8px">Roles a client cancelled in the last 7 days</div>
      %(churn_list)s</div>
  </div>
</div>

<div class="panel" id="health">
  <div class="phead">Client health</div>
  <div class="pcap">%(health_cap)s</div>
  %(client_health)s
</div>

<div class="panel" id="interview">
  <div class="phead">Interview</div>
  <div class="pcap">Everyone in the interview stage, grouped by where they are, most advanced first, longest-waiting first inside each. &#128308; stuck 14+ days, &#128992; stuck 7+ days. %(interview_cap_n)d candidates</div>
  %(interview_body)s
</div>

<div class="panel" id="assessment">
  <div class="phead">Assessment</div>
  <div class="pcap">Candidates in the assessment stage, grouped by stage in pipeline order, longest-waiting first within each. &#128308; stuck 14+ days, &#128992; stuck 7+ days. %(assessment_cap_n)d candidates</div>
  %(assessment_body)s
</div>

<div class="panel" id="shortage">
  <div class="phead">Candidate shortage</div>
  <div class="pcap">Roles with fewer than 3 candidates in play, grouped by how close they are to filling. In play = live viable candidates, Close to intro = fresh candidates at Approved Internally, Verification or Waiting For Interview, Sourcing = fresh candidates earlier in the funnel (all fresh pools moved in the last 14 days), Open = days since the role was created. Roles opened under 2 weeks ago are held back in their own greyed group at the bottom and are not judged. %(shortage_n)d roles short, %(shortage_new)d too new to judge</div>
  %(shortage_body)s
</div>

<div class="panel" id="close">
  <div class="phead">Close to hire</div>
  <div class="pcap">Every role whose fulfilment status is In Final Selection or In Trial, with the candidates sitting past interview completed (assessment, trial, negotiation, hired). Roles with nobody past interview come first, those are the ones at risk. &#128308; stuck 14+ days, &#128992; stuck 7+ days. %(close_roles)d roles, %(close_n)d candidates</div>
  %(close_body)s
</div>

</div>
<script>
document.querySelectorAll('.tab').forEach(function(b){
  b.addEventListener('click',function(){
    document.querySelectorAll('.tab').forEach(function(x){x.classList.remove('active')});
    document.querySelectorAll('.panel').forEach(function(x){x.classList.remove('active')});
    b.classList.add('active');
    document.getElementById(b.dataset.t).classList.add('active');
  });
});
</script>
</body></html>"""


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        import traceback
        log("ERROR  " + repr(e))
        log(traceback.format_exc())
        sys.exit(1)
