#!/usr/bin/env python3
"""Local dashboard for the Gemini Live news digest — card view + paste box.

Workflow: copy the digest JSON from the Claude routines page -> paste it into the
box here -> Save. It's parsed into structured findings, stored in Aiven Postgres,
and rendered as color-coded cards (area -> category), with a date selector.

Run:  venv/bin/python ~/Desktop/gemini-live-news/server.py
Open: http://localhost:8787
"""
import datetime as _dt
import html
import json
import os
import secrets
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

import psycopg2

PORT = int(os.environ.get("PORT", "8787"))

# DB creds: ENV VAR ONLY, never hardcoded in this file (so the DB password is
# never committed to git). Set AIVEN_DSN in the environment before running —
# locally via the launchd plist's EnvironmentVariables, on Render via its
# dashboard's Environment tab.
DSN = os.environ["AIVEN_DSN"]

# --- access token gate --------------------------------------------------
# Anyone with the URL must enter this token once; a session cookie then keeps
# them logged in indefinitely (until they clear cookies). The token is the
# single line in token.txt, next to this script — edit that file to change it.
_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.txt")
ACCESS_TOKEN = open(_TOKEN_FILE).read().strip()
_SESSIONS = set()  # valid session ids (in-memory; resets if the server restarts)

AREA_LABEL = {
    "gemini": "Gemini 3.1 Flash Live Preview",
    "gemini_livekit": "Gemini 3.1 Flash Live Preview × LiveKit",
}
CAT_ORDER = ["known_issue", "unresolved", "fixed", "developer_fix", "update"]
CAT_LABEL = {
    "known_issue": "Known Issues", "unresolved": "Yet to Be Solved",
    "fixed": "Already Fixed", "developer_fix": "Developer-Side Fixes", "update": "Updates",
}
CAT_COLOR = {
    "known_issue": "#ef4444", "unresolved": "#f59e0b", "fixed": "#22c55e",
    "developer_fix": "#3b82f6", "update": "#a78bfa",
}


def _conn():
    return psycopg2.connect(DSN)


def esc(x):
    return html.escape(str(x)) if x is not None else ""


# ---- parsing pasted digest -> findings ------------------------------------

def _extract_findings(text):
    """Robustly pull digest fields from pasted text: whole-object JSON first,
    else salvage the findings array via bracket matching."""
    text = text.strip()
    # 1) whole object
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1:
        try:
            obj = json.loads(text[s:e + 1])
            if isinstance(obj, dict) and obj.get("findings"):
                return obj.get("findings"), obj.get("tldr", ""), obj.get("digest_date")
        except Exception:
            pass
    # 2) salvage findings array
    idx = text.rfind('"findings"')
    if idx != -1:
        lb = text.find("[", idx)
        if lb != -1:
            depth = 0
            for i in range(lb, len(text)):
                if text[i] == "[":
                    depth += 1
                elif text[i] == "]":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[lb:i + 1]), "", None
                        except Exception:
                            return None, "", None
    return None, "", None


def save_paste(date_str, pasted):
    findings, tldr, parsed_date = _extract_findings(pasted)
    if not findings:
        return 0, "Could not find a findings array in what you pasted. Paste the digest JSON from the routine."
    # Use the date YOU picked in the form (defaults to today). Only fall back to the
    # JSON's digest_date if the form date is somehow empty. (The routine's JSON date is
    # in UTC and can read one day behind IST, so the form date is the source of truth.)
    date_str = (date_str or parsed_date or _dt.date.today().isoformat()).strip()
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM gemini_live_findings   WHERE digest_date=%s;", (date_str,))
        cur.execute("DELETE FROM gemini_live_digest_runs WHERE digest_date=%s;", (date_str,))
        cur.execute("INSERT INTO gemini_live_digest_runs (digest_date, status, tldr, raw_markdown) VALUES (%s,'ok',%s,%s);",
                    (date_str, tldr, pasted))
        for f in findings:
            cur.execute(
                "INSERT INTO gemini_live_findings "
                "(digest_date, area, category, title, description, developer_fix, status_note, reported_date, source_urls) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb);",
                (date_str, f.get("area"), f.get("category"), f.get("title"), f.get("description"),
                 f.get("developer_fix"), f.get("status_note"), f.get("reported_date"),
                 json.dumps(f.get("source_urls", []))),
            )
    c.close()
    return len(findings), date_str


# ---- reading ---------------------------------------------------------------

def all_dates():
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT DISTINCT digest_date FROM gemini_live_digest_runs ORDER BY digest_date DESC;")
        return [r[0] for r in cur.fetchall()]


def fetch(date_str):
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT digest_date, tldr, ran_at FROM gemini_live_digest_runs WHERE digest_date=%s;", (date_str,))
        run = cur.fetchone()
        cur.execute(
            "SELECT area, category, title, description, developer_fix, status_note, reported_date, source_urls "
            "FROM gemini_live_findings WHERE digest_date=%s ORDER BY area, category, id;", (date_str,))
        return run, cur.fetchall()


def render_sources(src):
    if isinstance(src, str):
        try:
            src = json.loads(src)
        except Exception:
            src = [src]
    if not src:
        return ""
    return '<div class="src">🔗 ' + " · ".join(f'<a href="{esc(u)}" target="_blank">{esc(u)}</a>' for u in src) + "</div>"


def render_body(sel_date_str, notice=""):
    dates = all_dates()
    today = _dt.date.today().isoformat()
    sel = sel_date_str or (str(dates[0]) if dates else today)

    form = (
        '<details class="add"><summary>➕ Paste a new digest (copy the JSON from the Claude routines page)</summary>'
        '<form method="POST" action="/add">'
        f'<label>Date <input type="date" name="date" value="{esc(today)}"></label>'
        '<textarea name="paste" required placeholder="Paste the digest JSON here (it can include surrounding text — I&#39;ll find the findings)."></textarea>'
        '<button type="submit">Save &amp; render</button>'
        '</form></details>'
    )
    if notice:
        form += f'<div class="notice">{esc(notice)}</div>'

    if dates:
        opts = "".join(
            f'<option value="{esc(d)}"{" selected" if str(d)==str(sel) else ""}>{esc(d)}'
            f'{" (latest)" if d==dates[0] else ""}</option>' for d in dates)
        bar = (f'<div class="bar">📅 <b>Date:</b> '
               f'<select onchange="location.href=\'/?date=\'+this.value">{opts}</select> '
               f'<span class="hint">{len(dates)} saved</span></div>')
    else:
        bar = '<div class="bar hint">No digests yet — paste your first one above.</div>'

    parts = ['<h1>Gemini 3.1 Flash Live Preview — Digest</h1>', form, bar]

    if dates:
        run, rows = fetch(str(sel))
        if run:
            parts.append(f'<div class="meta">Showing <b>{esc(run[0])}</b> · {len(rows)} findings · saved {esc(run[2])}</div>')
            if run[1]:
                parts.append(f'<div class="tldr"><b>TL;DR</b><br>{esc(run[1])}</div>')
        grouped = {}
        for area, cat, title, desc, dev, note, rdate, src in rows:
            grouped.setdefault(area, {}).setdefault(cat, []).append((title, desc, dev, note, rdate, src))
        for area in ("gemini", "gemini_livekit"):
            if area not in grouped:
                continue
            parts.append(f'<h2 class="area">{esc(AREA_LABEL.get(area, area))}</h2>')
            for cat in CAT_ORDER:
                if cat not in grouped[area]:
                    continue
                color = CAT_COLOR.get(cat, "#888")
                items = grouped[area][cat]
                parts.append(f'<h3 class="cat" style="border-color:{color}">'
                             f'<span class="dot" style="background:{color}"></span>'
                             f'{esc(CAT_LABEL.get(cat, cat))} <span class="count">{len(items)}</span></h3>')
                for title, desc, dev, note, rdate, src in items:
                    mbits = []
                    if rdate:
                        mbits.append(f"📅 {esc(rdate)}")
                    if note:
                        mbits.append(esc(note))
                    meta = f'<div class="fmeta">{" · ".join(mbits)}</div>' if mbits else ""
                    devl = f'<div class="devfix"><b>Dev fix:</b> {esc(dev)}</div>' if dev else ""
                    parts.append(f'<div class="card" style="border-left-color:{color}">'
                                 f'<div class="title">{esc(title)}</div>{meta}'
                                 f'<div class="desc">{esc(desc)}</div>{devl}{render_sources(src)}</div>')
    return "\n".join(parts)


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gemini Live Digest</title>
<style>
  body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:940px;margin:0 auto;
       padding:24px;background:#0b0e14;color:#e6e6e6;line-height:1.55}}
  h1{{font-size:22px;margin:0 0 14px}}
  a{{color:#7dd3fc;word-break:break-all}}
  .add{{background:#141a24;border:1px solid #232b39;border-radius:8px;padding:12px 14px;margin-bottom:10px}}
  .add summary{{cursor:pointer;font-weight:600}}
  .add form{{display:flex;flex-direction:column;gap:10px;margin-top:12px}}
  .add label{{font-size:13px;color:#8b95a5}}
  .add input[type=date]{{background:#0b0e14;color:#e6e6e6;border:1px solid #2b3547;border-radius:6px;padding:6px 8px;margin-left:8px}}
  .add textarea{{width:100%;min-height:180px;background:#0b0e14;color:#e6e6e6;border:1px solid #2b3547;
                 border-radius:8px;padding:10px;font-family:ui-monospace,Menlo,monospace;font-size:12px;resize:vertical}}
  .add button{{align-self:flex-start;background:#2563eb;color:#fff;border:0;border-radius:8px;padding:9px 18px;font-size:14px;cursor:pointer}}
  .notice{{background:#3a1f1f;border:1px solid #5c2b2b;color:#fca5a5;border-radius:8px;padding:10px 12px;margin:8px 0;font-size:13px}}
  .bar{{background:#141a24;border:1px solid #232b39;border-radius:8px;padding:10px 12px;margin:10px 0;font-size:14px}}
  .bar select{{background:#0b0e14;color:#e6e6e6;border:1px solid #2b3547;border-radius:6px;padding:5px 8px;margin:0 8px}}
  .hint{{color:#8b95a5;font-size:13px}}
  .meta{{color:#8b95a5;font-size:13px;margin:12px 0 6px}}
  .tldr{{background:#141a24;border:1px solid #232b39;border-radius:10px;padding:14px 16px;margin-bottom:8px}}
  h2.area{{margin:26px 0 8px;font-size:18px;border-bottom:1px solid #232b39;padding-bottom:6px}}
  h3.cat{{display:flex;align-items:center;gap:8px;font-size:15px;margin:18px 0 10px;border-left:4px solid;padding-left:10px}}
  .dot{{width:9px;height:9px;border-radius:50%;display:inline-block}}
  .count{{color:#8b95a5;font-weight:400;font-size:13px}}
  .card{{background:#121722;border:1px solid #212938;border-left:4px solid;border-radius:8px;padding:12px 14px;margin:0 0 10px}}
  .title{{font-weight:600;margin-bottom:4px}}
  .fmeta{{color:#8b95a5;font-size:12px;margin-bottom:6px}}
  .desc{{font-size:14px;white-space:pre-wrap}}
  .devfix{{margin-top:8px;background:#0f1a12;border:1px solid #1f3a25;border-radius:6px;padding:8px 10px;font-size:14px}}
  .src{{margin-top:8px;font-size:12px;color:#8b95a5}}
</style></head><body>{body}</body></html>"""


LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gemini Live Digest — Login</title>
<style>
  body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0b0e14;color:#e6e6e6;
       display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
  .box{{background:#141a24;border:1px solid #232b39;border-radius:12px;padding:32px;width:320px}}
  h1{{font-size:18px;margin:0 0 18px}}
  input{{width:100%;box-sizing:border-box;background:#0b0e14;color:#e6e6e6;border:1px solid #2b3547;
        border-radius:8px;padding:10px 12px;font-size:14px;margin-bottom:12px}}
  button{{width:100%;background:#2563eb;color:#fff;border:0;border-radius:8px;padding:10px;font-size:14px;cursor:pointer}}
  .err{{color:#fca5a5;font-size:13px;margin-bottom:12px}}
</style></head><body>
  <div class="box">
    <h1>🔒 Gemini Live Digest</h1>
    <form method="POST" action="/login">
      {error}
      <input type="password" name="token" placeholder="Enter access token" autofocus required>
      <button type="submit">Enter</button>
    </form>
  </div>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, code=200, headers=None):
        data = body.encode("utf-8")
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if not headers or "Content-Type" not in headers:
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _is_authed(self):
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return False
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        sid = cookie.get("sid")
        return bool(sid and sid.value in _SESSIONS)

    def do_GET(self):
        p = urlparse(self.path)
        q = parse_qs(p.query)

        # Keep-alive health check — NO auth, NO DB, cheap. An external cron pings
        # this every ~5 min so Render's free tier never spins down (15-min idle).
        if p.path in ("/health", "/healthz"):
            self._send("ok", 200, {"Content-Type": "text/plain; charset=utf-8"})
            return

        if p.path == "/login":
            err = '<div class="err">Wrong token, try again.</div>' if q.get("err") else ""
            self._send(LOGIN_PAGE.format(error=err))
            return

        if p.path not in ("/", "/index.html"):
            self._send("not found", 404); return

        if not self._is_authed():
            self._send("", 303, {"Location": "/login"}); return

        try:
            body = render_body(q.get("date", [None])[0], notice=q.get("err", [""])[0])
        except Exception as e:
            body = f"<h1>DB error</h1><pre>{esc(e)}</pre>"
        self._send(PAGE.format(body=body))

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(length).decode("utf-8"))

        if path == "/login":
            token = form.get("token", [""])[0]
            if secrets.compare_digest(token, ACCESS_TOKEN):
                sid = secrets.token_urlsafe(24)
                _SESSIONS.add(sid)
                # 10-year cookie so a logged-in browser effectively never has to re-enter
                # the token (note: in-memory _SESSIONS still resets if the server restarts).
                self._send("", 303, {"Location": "/", "Set-Cookie": f"sid={sid}; HttpOnly; Path=/; Max-Age=315360000"})
            else:
                self._send("", 303, {"Location": "/login?err=1"})
            return

        if path != "/add":
            self._send("not found", 404); return
        if not self._is_authed():
            self._send("", 303, {"Location": "/login"}); return

        date_str = (form.get("date", [""])[0] or _dt.date.today().isoformat()).strip()
        pasted = form.get("paste", [""])[0]
        try:
            n, result = save_paste(date_str, pasted)
            if n == 0:
                self._send("", 303, {"Location": f"/?err={quote(result)}"})
            else:
                self._send("", 303, {"Location": f"/?date={result}"})
        except Exception as e:
            self._send(PAGE.format(body=f"<h1>Save failed</h1><pre>{esc(e)}</pre><a href='/'>back</a>"), 500)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"Digest dashboard at http://localhost:{PORT}")
    print(f"Access token: {ACCESS_TOKEN}  (saved in .dashboard_token; set DASHBOARD_TOKEN env var to override)")
    # 0.0.0.0 so this also works when deployed (Render etc need to bind all interfaces);
    # still reachable at localhost when run on your own machine.
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
