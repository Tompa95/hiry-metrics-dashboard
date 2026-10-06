#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hiry Stage Board.

Read-only view of every candidate sitting in Interview, Assessment, Trial or
Negotiation, on active roles of active clients, excluding anyone rated
Rejected. One tab per stage, grouped by sub-status, longest-waiting first.
Every candidate has an "Open in Airtable" button that jumps straight to the
Placement Match record, which is where status, rating and comments get edited.

Deliberately NO write path: the page is public, so it holds no token and
cannot change Airtable. Comment text is also left off the page for the same
reason.

Reuses refresh_dashboard.py for Airtable access, the active-role rules, the
hidden-roles list and the GitHub publisher, so both boards always agree on
what "active" means. Publishes to stages/index.html in the same Pages repo.

Usage:
  python3 stage_board.py              build + publish
  python3 stage_board.py --no-publish build + archive only (for testing)
  python3 stage_board.py --no-ai       skip the comment summaries (fast)
"""

import os
import sys
from datetime import datetime

import refresh_dashboard as rd

PAGES_PATH = "stages/index.html"

# Airtable formula field fld763Wq4l7HvHOcU, on Days Since Status Changed:
#   <=2 days -> Status OK, <=4 -> Status Overdue, else -> Status Long Overdue.
# Pulled as-is (not recomputed) so the board follows if the rule changes.
F_LEVEL = "Status - Level"
RECORD_URL = "https://airtable.com/{base}/{table}/{rec}"

# (tab key, tab label, accent colour, [(Airtable status, sub-status label), ...])
# Order inside each stage is pipeline order. Dead outcomes (assessment ghosted
# or failed, trial failed) are intentionally not listed, so they never show.
STAGES = [
    ("interview", "Interview", "#06b6d4", [
        ("09 - Interview - To Schedule", "To schedule"),
        ("095 - Interview - Link Sent", "Link sent"),
        ("10 - Interview - Scheduled", "Scheduled"),
        ("11 - Interview - Completed", "Completed"),
    ]),
    ("assessment", "Assessment", "#8b5cf6", [
        ("13 - Assessment To Send", "To send"),
        ("14 - Assessment Shared", "Shared"),
        ("15 - Assessment In Progress", "In progress"),
        ("16 - Assessment Completed", "Completed"),
    ]),
    ("trial", "Trial", "#f59e0b", [
        ("13 - Trial To Start", "To start"),
        ("13 - Trial In Progress", "In progress"),
        ("14 - Trial Completed", "Completed"),
    ]),
    ("negotiation", "Negotiation", "#ec4899", [
        ("12 - Negotiation", "In negotiation"),
    ]),
]


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def pull_rows(at):
    """Return (rows, n_active_roles). Pulls only the matches in the wanted
    statuses (filtered server side), so a run takes seconds, not minutes."""
    clients = at.list_all(
        rd.T_CLIENTS,
        fields=[rd.F_CLIENT_NAME, rd.F_CLIENT_FULFILL],
        filter_formula="{%s}='Fulfillment - Active'" % rd.F_CLIENT_FULFILL,
    )
    active_clients = {c["id"] for c in clients}

    placements = at.list_all(rd.T_PLACEMENTS,
                             fields=[rd.F_PL_STATUS, rd.F_PL_HEADLINE, rd.F_PL_CLIENT])
    roles = {}
    for p in placements:
        f = p["fields"]
        if (f.get(rd.F_PL_STATUS) in rd.ACTIVE_PLACEMENT_STATUSES
                and rd.link_id(f.get(rd.F_PL_CLIENT)) in active_clients
                and p["id"] not in rd.EXCLUDED_PLACEMENT_IDS):
            roles[p["id"]] = f.get(rd.F_PL_HEADLINE) or "(no headline)"

    # TRIM() because some choices carry a trailing space ("12 - Negotiation ").
    wanted = [st for _, _, _, subs in STAGES for st, _ in subs]
    formula = "OR(%s)" % ",".join("TRIM({%s})='%s'" % (rd.F_M_STATUS, st) for st in wanted)
    matches = at.list_all(
        rd.T_MATCHES,
        fields=[rd.F_M_NAME, rd.F_M_STATUS, rd.F_M_RATING, rd.F_M_DAYS, rd.F_M_POSITION, F_LEVEL],
        filter_formula=formula,
    )

    rows = []
    for m in matches:
        f = m["fields"]
        if rd.is_rejected(f):
            continue
        pid = rd.link_id(f.get(rd.F_M_POSITION))
        if pid not in roles:
            continue
        rating = (f.get(rd.F_M_RATING) or "Candidate - Unrated").replace("Candidate - ", "")
        rows.append({
            "id": m["id"],
            "name": rd.name_of(f) or "(no name)",
            "role": roles[pid],
            "status": rd.status_of(f),
            "rating": rating,
            "days": rd.days_of(f),
            "level": (f.get(F_LEVEL) or "").strip(),
        })
    return rows, len(roles)


# ---------------------------------------------------------------------------
# AI notes: one-line summary of the LATEST comment + last call date
# ---------------------------------------------------------------------------
# Uses the Claude Code CLI already logged in on Tom's Mac (headless "-p"), so
# no API key is needed. One batched call for every candidate. If anything
# fails (CLI missing, timeout, bad JSON) the board still publishes, just
# without notes. The AI is never allowed to block the board.
CLAUDE_BIN = os.environ.get("HIRY_CLAUDE_BIN") or os.path.expanduser("~/.local/bin/claude")
AI_TIMEOUT = 420

AI_PROMPT = """You are given candidates from a recruiting pipeline, each with their Airtable comments, newest first, dates in UTC.
For EACH candidate return:
- "summary": one short line, max 18 words, summarising ONLY the most recent comment, plain and factual. "" if there are no comments.
- "call_date": the date of the most recent interview or call that the comments say HAPPENED or IS BOOKED, as YYYY-MM-DD. Resolve relative words like "today", "Wed" or "tomorrow" against that comment's own date. null if no call date is stated. Never invent a date.
Never use em dashes or en dashes. Output ONLY a JSON object mapping id to {"summary": ..., "call_date": ...}. No prose, no code fences.

TODAY: %s
DATA:
%s"""


def fetch_comments(at, base, rec):
    url = "https://api.airtable.com/v0/%s/%s/%s/comments?pageSize=100" % (base, rd.T_MATCHES, rec)
    return at._get(url).get("comments", [])


def clean_ai(text):
    """Model output goes on a public page: strip dashes the publish guard would
    reject, collapse whitespace, cap length."""
    t = str(text or "").replace("—", ", ").replace("–", "-")
    t = " ".join(t.split())
    return t[:160]


def call_when(iso):
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None, None
    if d == rd.TODAY:
        return d, "today"
    return d, ("upcoming" if d > rd.TODAY else "past")


def ai_notes(at, base, rows):
    """Return {record_id: {"summary": str, "date": date|None, "when": str|None}}."""
    import json
    import subprocess

    if not os.path.exists(CLAUDE_BIN):
        rd.log("Stage board: AI skipped, Claude CLI not found at %s" % CLAUDE_BIN)
        return {}

    items = []
    for r in rows:
        try:
            cs = fetch_comments(at, base, r["id"])
        except Exception as e:
            rd.log("Stage board: comments failed for %s (%s)" % (r["id"], e.__class__.__name__))
            cs = []
        items.append({
            "id": r["id"], "candidate": r["name"], "role": r["role"], "status": r["status"],
            "comments_newest_first": [{"date": c.get("createdTime", "")[:16],
                                       "text": (c.get("text") or "")[:1200]} for c in cs],
        })
        r["n_comments"] = len(cs)

    prompt = AI_PROMPT % (rd.TODAY.isoformat(), json.dumps(items, ensure_ascii=False))
    try:
        out = subprocess.run([CLAUDE_BIN, "-p", "--model", "haiku"], input=prompt,
                             capture_output=True, text=True, timeout=AI_TIMEOUT,
                             cwd=os.path.dirname(os.path.abspath(__file__)))
    except Exception as e:
        rd.log("Stage board: AI skipped (%s)" % e.__class__.__name__)
        return {}
    if out.returncode != 0:
        rd.log("Stage board: AI skipped, CLI exit %d: %s" % (out.returncode, out.stderr.strip()[:200]))
        return {}

    raw = out.stdout.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").split("\n", 1)[-1]
    raw = raw[raw.find("{"): raw.rfind("}") + 1]
    try:
        parsed = json.loads(raw)
    except ValueError:
        rd.log("Stage board: AI skipped, could not parse output: %r" % out.stdout[:200])
        return {}

    notes = {}
    for rid, v in parsed.items():
        if not isinstance(v, dict):
            continue
        d, when = call_when(v.get("call_date"))
        notes[rid] = {"summary": clean_ai(v.get("summary")), "date": d, "when": when}
    rd.log("Stage board: AI notes for %d of %d candidates" % (len(notes), len(rows)))
    return notes


# ---------------------------------------------------------------------------
# Notes cache: the morning (AI) run saves its notes next to the board, and a
# manual GitHub refresh (no Claude login there) reuses them. Same content that
# is already on the public page, so the JSON exposes nothing new.
# ---------------------------------------------------------------------------
NOTES_PATH = "stages/notes.json"
REFRESH_WORKFLOW = "refresh-stage-board.yml"


def notes_to_json(notes):
    import json
    return json.dumps({
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "notes": {rid: {"summary": n["summary"],
                        "call_date": n["date"].isoformat() if n["date"] else None}
                  for rid, n in notes.items()},
    }, ensure_ascii=False, indent=1)


def load_cached_notes(cfg):
    """Return (notes, generated_at) from the last AI run, or ({}, None)."""
    import base64
    import json
    import urllib.request
    url = "https://api.github.com/repos/%s/contents/%s?ref=main" % (cfg["repo"], NOTES_PATH)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + cfg["github_pat"],
        "Accept": "application/vnd.github+json", "User-Agent": "hiry-stage-board"})
    try:
        blob = json.load(urllib.request.urlopen(req, timeout=60))
        data = json.loads(base64.b64decode(blob["content"]).decode("utf-8"))
    except Exception as e:
        rd.log("Stage board: no cached notes (%s)" % e.__class__.__name__)
        return {}, None
    notes = {}
    for rid, v in data.get("notes", {}).items():
        d, when = call_when(v.get("call_date"))   # recomputed against TODAY
        notes[rid] = {"summary": clean_ai(v.get("summary")), "date": d, "when": when}
    return notes, data.get("generated")


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
def rating_chip(rating):
    cls = {"Approved": "ok", "Unrated": "un"}.get(rating, "mid")
    return '<span class="rate %s">%s</span>' % (cls, rd.esc(rating))


def level_pill(level):
    cls = "lo" if "Long" in level else ("ov" if "Overdue" in level else "okl")
    return '<span class="lvl %s">%s</span>' % (cls, rd.esc(level or "-"))


def note_line(r):
    """Second line under a candidate: call date badge + AI summary."""
    n = r.get("note")
    if n is None:
        return ""
    parts = []
    if n.get("date"):
        label = {"today": "Call today", "upcoming": "Call", "past": "Last call"}[n["when"]]
        parts.append('<span class="cd %s">&#128197; %s %s</span>'
                     % (n["when"], label, n["date"].strftime("%a ") + str(n["date"].day) + n["date"].strftime(" %b")))
    if n.get("summary"):
        parts.append('<span class="sm">%s</span>' % rd.esc(n["summary"]))
    elif not r.get("n_comments"):
        parts.append('<span class="sm none">No comments yet</span>')
    return '<div class="note">%s</div>' % "".join(parts) if parts else ""


def candidate_row(r, base):
    d = r["days"]
    days = ("%dd" % d) if d is not None else ""
    url = RECORD_URL.format(base=base, table=rd.T_MATCHES, rec=r["id"])
    return '<div class="cand">' + (
        '<div class="row">'
        '<span class="nm">%s</span>'
        '<span class="rl">%s</span>'
        '%s'
        '<span class="dy">%s</span>'
        '<a class="open" href="%s" target="_blank" rel="noopener">Open in Airtable</a>'
        '</div>'
        % (rd.esc(r["name"]), rd.esc(r["role"]), level_pill(r["level"]), days, url)
    ) + note_line(r) + '</div>'


def render(rows, n_roles, base, run_label="morning run", notes_source="", repo=""):
    by_status = {}
    for r in rows:
        by_status.setdefault(r["status"], []).append(r)
    for lst in by_status.values():
        lst.sort(key=lambda r: (-(r["days"] if r["days"] is not None else -1), r["name"].lower()))

    stat_cards, tabs, panels = [], [], []
    for i, (key, label, colour, subs) in enumerate(STAGES):
        stage_rows = [r for st, _ in subs for r in by_status.get(st, [])]
        total = len(stage_rows)
        n_long = sum(1 for r in stage_rows if "Long" in r["level"])
        n_over = sum(1 for r in stage_rows if "Overdue" in r["level"] and "Long" not in r["level"])
        stat_cards.append(
            '<div class="stat" style="border-top:3px solid %s"><div class="n">%d</div>'
            '<div class="l">%s</div><div class="ovl">&#128992; %d overdue &middot; &#128308; %d long overdue</div></div>'
            % (colour, total, label, n_over, n_long))
        tabs.append('<button class="tab%s" data-t="%s" style="--c:%s">%s <span class="tc">%d</span></button>'
                    % (" active" if i == 0 else "", key, colour, label, total))

        groups = []
        for st, sub_label in subs:
            lst = by_status.get(st, [])
            body = "".join(candidate_row(r, base) for r in lst) or \
                '<div class="empty">Nobody here right now</div>'
            groups.append(
                '<div class="grp" style="border-left-color:%s"><div class="gh">%s '
                '<span class="cnt">(%d)</span></div>%s</div>' % (colour, sub_label, len(lst), body))
        panels.append('<div class="panel%s" id="%s">%s</div>'
                      % (" active" if i == 0 else "", key, "".join(groups)))

    html = TEMPLATE
    for k, v in {
        "{{DATE}}": rd.pretty_date(rd.TODAY),
        "{{TIME}}": datetime.now().strftime("%H:%M"),
        "{{RUN}}": run_label,
        "{{NOTES_SRC}}": (" &middot; " + rd.esc(notes_source)) if notes_source else "",
        "{{REFRESH_URL}}": "https://github.com/%s/actions/workflows/%s" % (repo, REFRESH_WORKFLOW),
        "{{N_ROLES}}": str(n_roles),
        "{{N_TOTAL}}": str(len(rows)),
        "{{STATS}}": "".join(stat_cards),
        "{{TABS}}": "".join(tabs),
        "{{PANELS}}": "".join(panels),
    }.items():
        html = html.replace(k, v)
    return html


TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stage Board &middot; {{DATE}}</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--border:#e5e7eb;--ink:#1f2430;--muted:#6b7280;--shadow:0 1px 3px rgba(16,24,40,.06),0 1px 2px rgba(16,24,40,.04)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5}
.wrap{max-width:1080px;margin:0 auto;padding:28px 22px 60px}
h1{font-size:22px;margin:0 0 2px}
.sub{color:var(--muted);font-size:13px;margin-bottom:22px}
.top{display:flex;align-items:center;justify-content:space-between;gap:12px}
.refresh{font-size:13px;font-weight:600;color:#fff;background:#1f2430;padding:8px 14px;border-radius:8px;text-decoration:none;white-space:nowrap}
.refresh:hover{background:#374151}
.strip{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:22px}
.stat{background:var(--card);border:1px solid var(--border);border-radius:12px;box-shadow:var(--shadow);padding:16px 18px}
.stat .n{font-size:28px;font-weight:700}
.stat .l{color:var(--muted);font-size:12.5px}
.tabs{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px}
.tab{border:none;cursor:pointer;font:inherit;font-weight:600;font-size:13px;padding:9px 15px;border-radius:999px;background:#eceef1;color:#4b5563}
.tab.active{background:var(--c);color:#fff}
.tc{opacity:.75;font-weight:700;margin-left:2px}
.panel{display:none;background:var(--card);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow);padding:20px}
.panel.active{display:block}
.grp{border:1px solid var(--border);border-left:4px solid;border-radius:11px;padding:10px 14px;margin-bottom:12px}
.gh{font-weight:700;font-size:13.5px;margin-bottom:4px}
.cnt{color:var(--muted);font-weight:600}
.cand{border-top:1px solid #f4f5f7;padding:6px 0}
.cand:first-of-type{border-top:none}
.row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.3fr) 162px 40px 128px;align-items:center;gap:8px;font-size:12.5px}
.note{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:3px;font-size:12px;color:#4b5563}
.cd{font-weight:600;white-space:nowrap;padding:1px 7px;border-radius:6px}
.cd.today{background:#dcfce7;color:#15803d}.cd.upcoming{background:#eff6ff;color:#1d4ed8}.cd.past{background:#f3f4f6;color:#6b7280}
.sm{color:#4b5563}.sm.none{color:#9ca3af;font-style:italic}
.dot{text-align:center;font-size:11px}
.nm{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rl{color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dy{text-align:right;color:#4b5563;font-variant-numeric:tabular-nums}
.rate{font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px;text-align:center}
.rate.ok{background:#dcfce7;color:#15803d}.rate.un{background:#f3f4f6;color:#6b7280}.rate.mid{background:#fef3c7;color:#92400e}
.open{justify-self:end;font-size:12px;font-weight:600;color:#1d4ed8;background:#eff6ff;border:1px solid #bfdbfe;padding:4px 10px;border-radius:7px;text-decoration:none;white-space:nowrap}
.open:hover{background:#dbeafe}
.lvl{font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px;white-space:nowrap;text-align:center}
.lvl.okl{background:#ecfdf3;color:#15803d}.lvl.ov{background:#fff7ed;color:#c2410c}.lvl.lo{background:#fef2f2;color:#b91c1c}
.ovl{font-size:11.5px;color:var(--muted);margin-top:4px}
.empty{color:var(--muted);font-size:12.5px;padding:4px 0}
.foot{color:var(--muted);font-size:12px;margin-top:18px}
</style></head>
<body><div class="wrap">
<div class="top"><h1>Stage Board</h1><a class="refresh" href="{{REFRESH_URL}}" target="_blank" rel="noopener" title="Opens GitHub. Click Run workflow, then reload this page in about a minute">&#8635; Refresh data</a></div>
<div class="sub">{{DATE}} &middot; {{RUN}} at {{TIME}}{{NOTES_SRC}} &middot; {{N_TOTAL}} candidates across {{N_ROLES}} active roles &middot; rejected candidates excluded &middot; status level from Airtable: OK up to 2 days in stage, Overdue 3 to 4, Long Overdue 5+</div>
<div class="strip">{{STATS}}</div>
<div class="tabs">{{TABS}}</div>
{{PANELS}}
<div class="foot">Read-only. Notes under each candidate are an AI summary of the latest Airtable comment, and the call date is pulled from the comments, so double check anything important in Airtable. Use Open in Airtable to change status, rating or leave a comment, the board picks it up on the next refresh</div>
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    no_publish = "--no-publish" in sys.argv
    cfg = rd.load_config()
    at = rd.Airtable(cfg["airtable_pat"], cfg["base"])

    rd.log("Stage board: pulling Airtable data (read-only)...")
    rows, n_roles = pull_rows(at)
    manual = os.environ.get("GITHUB_ACTIONS") == "true"
    notes, fresh, source = {}, False, ""
    if "--no-ai" not in sys.argv:
        notes = ai_notes(at, cfg["base"], rows)
        fresh = bool(notes)
    if not notes:
        notes, generated = load_cached_notes(cfg)
        if notes:
            source = "notes from the %s run" % generated
            rd.log("Stage board: using cached notes from %s" % generated)
    for r in rows:
        if notes:
            r["note"] = notes.get(r["id"], {"summary": "", "date": None, "when": None})
    run_label = "manual refresh" if manual else "morning run"
    counts = {label: sum(1 for r in rows if r["status"] in {s for s, _ in subs})
              for _, label, _, subs in STAGES}
    rd.log("Stage board: %d candidates on %d active roles %s" % (len(rows), n_roles, counts))

    html = render(rows, n_roles, cfg["base"], run_label, source, cfg["repo"])
    if "—" in html or "–" in html:
        raise RuntimeError("Stage board HTML contains an em/en dash, aborting")

    try:
        os.makedirs(rd.DASHBOARDS_DIR, exist_ok=True)
        path = os.path.join(rd.DASHBOARDS_DIR, "(C) %s stage board.html" % rd.TODAY.isoformat())
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        rd.log("Stage board: local copy written: %s" % path)
    except Exception as e:
        rd.log("Stage board: local copy skipped (%s)" % e.__class__.__name__)

    if no_publish:
        rd.log("Stage board: --no-publish, not pushed")
        return 0

    sha = rd.publish_github(cfg["github_pat"], cfg["repo"], html, path=PAGES_PATH,
                            message="Stage board %s" % rd.TODAY.isoformat())
    if fresh:
        rd.publish_github(cfg["github_pat"], cfg["repo"], notes_to_json(notes), path=NOTES_PATH,
                          message="Stage board notes %s" % rd.TODAY.isoformat())
    rd.log("Stage board SUCCESS candidates=%d roles=%d %s notes=%s sha=%s"
           % (len(rows), n_roles, counts, "fresh" if fresh else (source or "none"), sha))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        import traceback
        rd.log("Stage board ERROR  " + repr(e))
        rd.log(traceback.format_exc())
        sys.exit(1)
