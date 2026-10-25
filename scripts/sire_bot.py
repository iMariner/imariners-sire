"""SIRE 2.0 field report bot. Runs in GitHub Actions every few minutes (no server, nothing on a laptop).

What it does each run:
  1. Reads new Telegram messages sent to the bot by the owner (long polling, no webhook).
  2. A pasted report goes to DeepSeek with field/extract_prompt.txt and comes back as one structured record.
     The owner gets a summary with Approve / Reject buttons. Replying to that summary with a correction
     re-reads the report with the correction applied.
  3. Approve: the record is written to field/reports/, the raw text to the private repo, field-data.json is rebuilt.
  4. Card proposals from Hermes (pull requests labelled "hermes-cards") are sent to the owner with Merge / Close.
     Merge: the PR is merged and sire-data.json is rebuilt.
  5. Any other seafarer can send a report too (paste, forward, or a PDF; photos and other files are declined). Their messages go to an inbox
     with no DeepSeek call. Once a day the inbox is cleaned up for free (split messages joined, exact and near
     duplicates dropped, non-SIRE text dropped) and only the unique reports are read, at most DAILY_READS per day.
     The owner gets the usual summaries plus one digest; senders are thanked when their report is published.
State (Telegram offset, pending reports, inbox) lives in the private repo, so no report text ever lands in public logs.

Environment: TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID, DEEPSEEK_API_KEY, DEEPSEEK_MODEL (optional),
PRIVATE_REPO_TOKEN (contents read/write on the private repo), GITHUB_TOKEN (this repo), REPO, RAW_REPO.
"""
import base64, datetime as dt, hashlib, html, io, json, os, random, re, secrets, subprocess, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = os.environ.get("REPO", "iMariner/imariners-sire")
RAW_REPO = os.environ.get("RAW_REPO", "iMariner/imariners-sire-raw")
TG = "https://api.telegram.org/bot" + os.environ["TELEGRAM_BOT_TOKEN"] + "/"
OWNER = int(os.environ["TELEGRAM_OWNER_ID"])
MODEL = os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat"
STATE_PATH = "state/bot.json"
SPLIT_LEN = 3500          # Telegram cuts long pastes into ~4096 char parts; a part this long probably continues
SEEN_PATH = "state/seen.json"     # fingerprints of every inbox report already read, so a repost is never read twice
DIGEST_HOUR = int(os.environ.get("SIRE_DIGEST_HOUR") or 7)    # UTC hour of the daily inbox run
DAILY_READS = int(os.environ.get("SIRE_DAILY_READS") or 15)   # DeepSeek reads per day at most; the rest wait a day
DAILY_FILES = 20          # screenshots and files passed on to the owner per day at most
SENDER_MSGS = 30          # messages one person can send per day
SENDER_CHARS = 80000      # characters one person can send per day
PDF_MAX_BYTES = 15_000_000   # bigger PDFs are asked for as text (Telegram bots can fetch 20 MB at most)
PDF_MAX_PAGES = 60
OCR_MAX_PAGES = 40           # scanned PDFs: pages read by OCR (tesseract), at most
MAX_REPORT = 40000        # a single report is cut at this length before it is read
JOIN_GAP = 180            # messages from one person this close together are one report
NEAR_DUP = 0.7            # word overlap above this counts as the same report
SITE = "https://imariners.com/sire-2-0/"
SIRE_WORDS = ["sire", "inspector", "inspection", "vetting", "viq", "ocimf", "observation", "obs", "chapter", "asked",
              "checked", "question", "master", "chief officer", "c/o", "2/o", "3/o", "chief engineer", "c/e", "2/e",
              "bridge", "engine room", "ecr", "ccr", "cargo", "mooring", "deck", "officer", "crew", "terminal", "port"]
TOPIC_KEYS = [t["k"] for t in json.loads((ROOT / "field" / "topics.json").read_text())]
QIDS = {r["id"] for r in json.loads((ROOT / "cards" / "meta.json").read_text())}


def log(*a):
    print(*a, flush=True)  # counts and ids only: this repo's logs are public


# ---------------------------------------------------------------- http helpers
def http(method, url, body=None, headers=None, timeout=60, retries=2):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", "User-Agent": "imariners-sire-bot"}
    h.update(headers or {})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=h), timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code in (404, 409, 422) or attempt == retries:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
        time.sleep(3 * (attempt + 1))


def tg(method, _timeout=40, _retries=2, **params):
    return http("POST", TG + method, params, timeout=_timeout, retries=_retries).get("result")


def tg_soft(method, **params):
    """Button answers and keyboard edits: Telegram rejects them once a tap is a few minutes old. Never fatal."""
    try:
        return tg(method, _timeout=10, _retries=0, **params)
    except Exception as e:
        log(f"{method} skipped ({type(e).__name__})")
        return None


def gh(method, path, token, body=None):
    return http(method, "https://api.github.com" + path, body,
                {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})


PRIV = os.environ["PRIVATE_REPO_TOKEN"]
PUB = os.environ["GITHUB_TOKEN"]


def priv_get(path):
    try:
        r = gh("GET", f"/repos/{RAW_REPO}/contents/{path}", PRIV)
        return base64.b64decode(r["content"]).decode(), r["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        raise


def priv_put(path, text, message, sha=None):
    body = {"message": message, "content": base64.b64encode(text.encode()).decode()}
    if sha:
        body["sha"] = sha
    return gh("PUT", f"/repos/{RAW_REPO}/contents/{path}", PRIV, body)


# ---------------------------------------------------------------- record handling
def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def extract(text, correction=""):
    system = (ROOT / "field" / "extract_prompt.txt").read_text()
    user = text if not correction else text + "\n\n---\nCorrections from the person who sent this report (apply them):\n" + correction
    r = http("POST", "https://api.deepseek.com/chat/completions",
             {"model": MODEL, "temperature": 0, "max_tokens": 8192, "response_format": {"type": "json_object"},
              "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
             {"Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"]}, timeout=180)
    choice = r["choices"][0]
    if choice.get("finish_reason") == "length":
        raise RuntimeError("DeepSeek reply was cut off (report too long)")
    return json.loads(choice["message"]["content"])


def declared_counts(text):
    """Entries that an Observation Declaration form states in plain words. These labels are never repeated for one
    finding, so they give a free check on what the AI listed. None for any other kind of report."""
    if not re.search(r"observation\s+declaration", text, re.I):
        return None
    return {"positives": len(re.findall(r"exceeded\s+normal\s+expect", text, re.I)),
            "photos": len(re.findall(r"photo\s+not\s+representative", text, re.I))}


def missing_entries(text, recs):
    """What the AI left out of a declaration form, as a sentence ('' when it all matches)."""
    want = declared_counts(text)
    if not want or len(recs) != 1:
        return ""
    r = recs[0]
    got_photos = sum(1 for o in r.get("observations") or [] if re.search(r"photo", str(o.get("text")), re.I))
    gaps = []
    if len(r.get("positives") or []) < want["positives"]:
        gaps.append(f"the form has {want['positives']} entries labelled 'Exceeded normal expectation' (positives), you listed {len(r.get('positives') or [])}")
    if got_photos < want["photos"]:
        gaps.append(f"the form has {want['photos']} entries labelled 'Photo not representative', you listed {got_photos} photo observations")
    return "; ".join(gaps)


def normalise(rec):
    d = {"date": "", "inspector": "", "port": "", "country": "", "terminal": "", "vessel_type": "Unknown", "hours": None,
         "start": None, "end": None, "schedule": [], "observations_total": None, "high_risk": None, "observations": [],
         "positives": [], "style": [], "questions": [], "checks": [], "depth": {}, "tips": [], "summary": ""}
    for k, v in d.items():
        if rec.get(k) in (None, "") and v not in (None, ""):
            rec[k] = v
        rec.setdefault(k, v)
    rec["depth"] = {k: max(0, min(3, int(round(float(v))))) for k, v in (rec.get("depth") or {}).items()
                    if k in TOPIC_KEYS and isinstance(v, (int, float))}
    for arr in ("observations", "questions", "checks"):
        rec[arr] = [x for x in rec[arr] if isinstance(x, dict)]
        for x in rec[arr]:
            if x.get("qid") not in QIDS:
                x["qid"] = ""
    rec["positives"] = [str(x) for x in rec["positives"] if x]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", rec.get("date") or ""):
        rec["date"] = ""
    place = rec.get("port") or rec.get("terminal") or "unknown-port"
    rec["id"] = "-".join(x for x in [rec["date"] or "undated", slug(place), slug(rec.get("inspector")) or "unknown-inspector"] if x)
    return rec


def free_report_id(rid):
    n, cand = 1, rid
    while (ROOT / "field" / "reports" / f"{cand}.json").exists():
        n += 1
        cand = f"{rid}-{n}"
    return cand


def summary_html(rec, key, note=""):
    e = lambda s: html.escape(str(s if s is not None else ""))
    cut = lambda t, n: t if n is None or len(str(t)) <= n else str(t)[:n].rsplit(" ", 1)[0] + "..."
    missing = [k for k in ("inspector", "port", "date") if not rec.get(k)]
    place = ", ".join(x for x in [rec.get("port"), rec.get("terminal"), rec.get("country")] if x) or "?"

    def build(n_obs, n_q, n_style, width):    # long reports are shortened step by step so the summary and buttons text always fit
        lines = [f"<b>SIRE report</b>  <code>{key}</code>"] + ([f"<i>{e(note)}</i>"] if note else []) + [
                 f"Inspector: <b>{e(rec.get('inspector') or '?')}</b>",
                 f"Port: {e(place)}   Date: {e(rec.get('date') or '?')}   Ship: {e(rec.get('vessel_type'))}",
                 f"Observations: {e(rec.get('observations_total') if rec.get('observations_total') is not None else len(rec['observations']))}   Positives: {len(rec['positives'])}"
                 f"   Questions: {len(rec['questions'])}   Checks: {len(rec['checks'])}",
                 "Depth: " + (", ".join(f"{k} {v}" for k, v in sorted(rec["depth"].items(), key=lambda kv: -kv[1])) or "none")]
        if rec["observations"]:
            lines.append("\n<b>Observations</b>")
            lines += [f"- {e(cut(o.get('text'), width))} [{e(o.get('qid') or 'no match')}]" for o in rec["observations"][:n_obs]]
            if len(rec["observations"]) > n_obs:
                lines.append(f"... and {len(rec['observations']) - n_obs} more")
        if rec["positives"]:
            lines.append("\n<b>Positives</b>")
            lines += [f"- {e(cut(x, width))}" for x in rec["positives"][:6]]
        if rec["questions"]:
            lines.append("\n<b>Questions</b>")
            lines += [f"- {e(q.get('rank') or '-')}: {e(cut(q.get('q'), width))} [{e(q.get('qid') or 'no match')}]" for q in rec["questions"][:n_q]]
            if len(rec["questions"]) > n_q:
                lines.append(f"... and {len(rec['questions']) - n_q} more")
        if rec["style"] and n_style:
            lines.append("\n<b>Style</b>")
            lines += [f"- {e(cut(s, width))}" for s in rec["style"][:n_style]]
        lines.append(f"\n{e(rec.get('summary'))}")
        if missing:
            lines.append(f"\n<i>Missing: {', '.join(missing)}. Reply to this message to add them.</i>")
        lines.append("<i>Reply to this message with any correction before approving.</i>")
        return "\n".join(lines)

    for args in [(12, 8, 5, None), (12, 8, 3, 230), (12, 6, 3, 160), (12, 4, 2, 110), (12, 3, 0, 80)]:
        out = build(*args)
        if len(out) < 3900:
            return out
    return out[:3850] + "\n... (cut)"


def keyboard(*buttons):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}


# ---------------------------------------------------------------- main
def load_state():
    raw_state, state_sha = priv_get(STATE_PATH)
    state = json.loads(raw_state) if raw_state else {}
    state.setdefault("offset", 0)
    state.setdefault("pending", {})
    state.setdefault("buffer", [])
    state.setdefault("notified_prs", [])
    state.setdefault("inbox", [])          # messages from other seafarers, read once a day
    state.setdefault("senders", {})        # per person per day: message and character counts, last thank-you
    state.setdefault("digest_day", "")
    return state, state_sha


def save_state(state, sha):
    r = priv_put(STATE_PATH, json.dumps(state, indent=1), "Bot state", sha)
    return ((r or {}).get("content") or {}).get("sha", sha)


def main():
    """One pass (GitHub Actions mode)."""
    state, sha = load_state()
    updates = tg("getUpdates", offset=state["offset"], timeout=0, allowed_updates=["message", "callback_query"]) or []
    cycle(state, updates, split_wait=90, check_prs=True)
    if digest_due(state):
        digest(state)
    save_state(state, sha)
    log(f"done: pending {len(state['pending'])}, buffered {len(state['buffer'])}")


def loop():
    """Always-on mode (VPS service): long polling, replies within seconds."""
    state, sha = load_state()
    last_prs, last_alert, last_pull = 0, 0, 0
    me = Path(__file__).read_bytes()
    log("loop started")
    while True:
        try:
            wait = 15 if state["buffer"] else 50
            updates = tg("getUpdates", _timeout=wait + 20, offset=state["offset"], timeout=wait,
                         allowed_updates=["message", "callback_query"]) or []
            due_prs = time.time() - last_prs > 600
            if time.time() - last_pull > 900:
                # pick up new code: if this file changed on GitHub, exit and let systemd restart the service
                last_pull = time.time()
                run("git", "pull", "--rebase", "--autostash", "-q")
                if Path(__file__).read_bytes() != me:
                    log("new bot code pulled, restarting")
                    save_state(state, sha)
                    sys.exit(0)
            due_digest = digest_due(state)
            if updates or state["buffer"] or due_prs or due_digest:
                before = json.dumps(state, sort_keys=True)
                if state["buffer"] or updates or due_digest:
                    run("git", "pull", "--rebase", "--autostash", "-q")
                cycle(state, updates, split_wait=12, check_prs=due_prs)
                if due_digest:
                    digest(state)
                if due_prs:
                    last_prs = time.time()
                if json.dumps(state, sort_keys=True) != before:
                    sha = save_state(state, sha)
        except Exception as e:
            log("loop error", type(e).__name__, str(e)[:200])
            if time.time() - last_alert > 1800:
                last_alert = time.time()
                try:
                    tg("sendMessage", chat_id=OWNER, text=f"SIRE bot hit an error and keeps running: {type(e).__name__}: {str(e)[:200]}")
                except Exception:
                    pass
            time.sleep(15)
            try:
                state, sha = load_state()
            except Exception:
                pass


def cycle(state, updates, split_wait, check_prs):
    changed_reports, changed_cards, purge = False, False, False
    log(f"updates: {len(updates)}")
    # Acknowledge every button tap first, before any slow work (reading reports, git), so the button never hangs.
    ACK = {"ok": "Publishing...", "no": "Rejecting...", "merge": "Merging...", "close": "Closing..."}
    for u in updates:
        cq = u.get("callback_query")
        if cq and cq["from"]["id"] == OWNER:
            tg_soft("answerCallbackQuery", callback_query_id=cq["id"], text=ACK.get(cq.get("data", "").split(":")[0], "Working..."))
    texts = []
    for u in updates:
        state["offset"] = u["update_id"] + 1
        if "callback_query" in u:
            cq = u["callback_query"]
            if cq["from"]["id"] != OWNER:
                continue
            try:
                handled = handle_callback(cq, state)
            except Exception as e:
                log(f"callback failed ({type(e).__name__})")
                tg_soft("sendMessage", chat_id=OWNER, text=f"That button failed ({type(e).__name__}). Try again.")
                continue
            changed_reports |= handled == "report"
            changed_cards |= handled == "cards"
            continue
        m = u.get("message") or {}
        fid = (m.get("from") or {}).get("id")
        if fid and fid != OWNER and m.get("chat", {}).get("type") == "private" and not (m.get("from") or {}).get("is_bot"):
            try:
                public_message(m, state)
            except Exception as e:
                log(f"public message failed ({type(e).__name__})")
            continue
        if fid != OWNER or m.get("chat", {}).get("type") != "private":
            # diagnostics without printing ids (logs are public)
            bot_id = int(os.environ["TELEGRAM_BOT_TOKEN"].split(":")[0]) if ":" in os.environ["TELEGRAM_BOT_TOKEN"] else 0
            log(f"ignored message: owner_match={fid == OWNER} chat={m.get('chat', {}).get('type')} "
                f"sender_digits={len(str(fid))} owner_digits={len(str(OWNER))} owner_is_bot_id={OWNER == bot_id}")
            continue
        text = (m.get("text") or m.get("caption") or "").strip()
        f = file_of(m)
        if f and f["kind"] == "pdf":
            body = pdf_text(f["fid"])
            if len(body) < 200:
                tg("sendMessage", chat_id=OWNER, text="I could not read text from that PDF (scanned pages, locked or too big). Paste the text of the report instead.")
                continue
            texts.append({"t": (text + "\n" + body).strip(), "date": m.get("date", 0), "id": m["message_id"], "pdf": True})   # one whole report, never a split paste
            continue
        elif f or m.get("photo"):
            tg("sendMessage", chat_id=OWNER, text="I can read text and PDF files only. Paste the report, or send it as a PDF.")
            continue
        if not text:
            continue
        if text.startswith("/"):
            command(text, state)
            continue
        reply_to = (m.get("reply_to_message") or {}).get("message_id")
        if reply_to:
            key = next((k for k, p in state["pending"].items() if p.get("msg_id") == reply_to), None)
            if key:
                correct(key, text, state)
                continue
        texts.append({"t": text, "date": m.get("date", 0), "id": m["message_id"]})

    # join Telegram's split parts back into one report
    queue = state["buffer"] + texts
    state["buffer"], reports, cur = [], [], []
    for part in queue:
        if cur and len(cur[-1]["t"]) >= SPLIT_LEN and part["date"] - cur[-1]["date"] <= 120 and not (part.get("pdf") or cur[-1].get("pdf")):
            cur.append(part)
        else:
            if cur:
                reports.append(cur)
            cur = [part]
    if cur:
        if len(cur[-1]["t"]) >= SPLIT_LEN and not cur[-1].get("pdf") and time.time() - cur[-1]["date"] < split_wait:
            state["buffer"] = cur        # more parts may still be on the way
        else:
            reports.append(cur)
    for parts in reports:
        new_report("\n".join(p["t"] for p in parts), state)

    if check_prs:
        changed_cards |= notify_prs(state)

    # keep at most 60 pending reports
    if len(state["pending"]) > 60:
        for k in sorted(state["pending"], key=lambda k: state["pending"][k]["created"])[:-60]:
            del state["pending"][k]

    # Only source files are committed here. The "Rebuild SIRE data" GitHub workflow rebuilds sire-data.json and
    # field-data.json and purges the CDN on every push, so the bot never races it on generated files.
    if changed_reports:
        run("git", "add", "field/reports", "field/lists")
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode:
            run("git", "commit", "-q", "-m", "Bot: new field report")
            push_with_retry()




def run(*cmd):
    subprocess.run(cmd, check=True, cwd=ROOT)


def push_with_retry():
    for attempt in range(4):
        if subprocess.run(["git", "push", "-q"], cwd=ROOT).returncode == 0:
            return
        log(f"push rejected, rebasing (attempt {attempt + 1})")
        if subprocess.run(["git", "pull", "--rebase", "--autostash", "-q"], cwd=ROOT).returncode:
            subprocess.run(["git", "rebase", "--abort"], cwd=ROOT)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("could not push the report to GitHub after 4 tries")


def command(text, state):
    cmd = text.split()[0].split("@")[0].lower()
    if cmd in ("/start", "/help"):
        msg = ("Paste or forward a SIRE 2.0 inspection report as text. I read it, show you a summary, and publish it "
               "to imariners.com when you tap Approve.\nReply to a summary to correct it (for example: port is Houston).\n"
               "/status  what is in the database\n/pending  reports waiting for you\n"
               f"/inbox  read what other seafarers sent now (otherwise daily at {DIGEST_HOUR:02d}:00 UTC)")
    elif cmd == "/status":
        f = json.loads((ROOT / "field-data.json").read_text()) if (ROOT / "field-data.json").exists() else {}
        msg = (f"Reports: {f.get('n_reports', 0)}\nInspectors: {f.get('n_inspectors', 0)}\nReal questions: {f.get('n_questions', 0)}\n"
               f"Latest: {f.get('updated') or '-'}\nWaiting for you: {len(state['pending'])}\n"
               f"Inbox from other seafarers: {len(state['inbox'])} messages from {len({i['chat'] for i in state['inbox']})} people")
    elif cmd == "/inbox":
        digest(state, manual=True)
        return
    elif cmd == "/pending":
        msg = "\n".join(f"{k}: {p['record'].get('inspector') or '?'}, {p['record'].get('port') or '?'}" for k, p in state["pending"].items()) or "Nothing waiting."
    else:
        msg = "Unknown command. /help"
    tg("sendMessage", chat_id=OWNER, text=msg)


def new_report(text, state):
    if len(text) < 200:
        tg("sendMessage", chat_id=OWNER, text="That is too short to be an inspection report. Paste the full report.")
        return
    tg("sendMessage", chat_id=OWNER, text="Reading the report...")
    stage(text, "", state)


def stage(text, correction, state, old_key=None, inbox=None):
    """Read one report and send the owner a summary per inspection. inbox = {"from": [chat ids], "note": str} for a
    report from the inbox: then problems are counted in the digest instead of messaged, and an inspection that is
    already on the page or already waiting is skipped. Returns what happened, for the digest."""
    old = state["pending"].get(old_key) or {}
    if inbox is None and old.get("from"):
        inbox = {"from": old["from"], "note": old.get("note", ""), "correcting": True}
    quiet = inbox is not None and not inbox.get("correcting")
    say = (lambda t: None) if quiet else (lambda t: tg("sendMessage", chat_id=OWNER, text=t))
    try:
        res = extract(text, correction)
    except Exception as e:
        log("extract failed", type(e).__name__)
        say(f"Could not read it ({type(e).__name__}: {str(e)[:200]}). Send it again later.")
        return "failed"
    if not res.get("is_sire_report"):
        say(f"Not a SIRE report: {res.get('reason', '')}\nTopic: {res.get('topic_hint', '')}")
        return "not_sire"
    if res.get("kind") == "question_list" or res.get("list"):
        return stage_list(text, res.get("list") or {}, correction, state, old_key, inbox)
    recs = res.get("reports") or ([res["report"]] if res.get("report") else [])
    recs = [r for r in recs if isinstance(r, dict)]
    if not recs:
        say("I could not find an inspection in that text. Send it again with more detail.")
        return "not_sire"
    warn = ""
    if declared_counts(text) is not None:     # an official declaration form: the count is simply what was listed
        for r in recs:
            r["observations_total"] = len(r.get("observations") or [])
    gap = missing_entries(text, recs)
    if gap:
        try:    # ask once more, telling the AI exactly what it missed
            again = extract(text, (correction + "\n" if correction else "") + "You left entries out: " + gap + ". Read the form again and include every entry.")
            recs2 = [r for r in (again.get("reports") or ([again["report"]] if again.get("report") else [])) if isinstance(r, dict)]
            if recs2 and len(missing_entries(text, recs2)) <= len(gap):
                recs, gap = recs2, missing_entries(text, recs2)
        except Exception as e:
            log("re-read failed", type(e).__name__)
        if gap:
            warn = "Check this one against the form: " + gap + "."
    if quiet:
        # free check after reading: same date, port and inspector as a published or waiting report
        waiting = {p["record"].get("id") for p in state["pending"].values()}
        known = lambda r: (r["date"] and r.get("port") and r.get("inspector")
                           and ((ROOT / "field" / "reports" / f"{r['id']}.json").exists() or r["id"] in waiting))
        normed, recs = [normalise(r) for r in recs], []
        for r in normed:
            if not known(r):
                recs.append(r)
                continue
            key = next((k for k, p in state["pending"].items() if p["record"].get("id") == r["id"]), None)
            if key:     # already waiting for the owner: this sender is thanked when it is approved
                f = state["pending"][key].setdefault("from", [])
                f += [c for c in inbox["from"] if c not in f]
            else:       # already published
                for chat in inbox["from"]:
                    tg_soft("sendMessage", chat_id=chat, disable_web_page_preview=True,
                            text=f"Thank you. That inspection is already on {SITE}")
        if not recs:
            return "on_page"
    if old_key and len(recs) > 1:
        # a correction to one summary: keep only that inspection
        want = slug((state["pending"].get(old_key) or {}).get("record", {}).get("inspector"))
        recs = [r for r in recs if slug(r.get("inspector")) == want][:1] or recs[:1]
    if len(recs) > 1 and not old_key:
        tg("sendMessage", chat_id=OWNER, text=f"I found {len(recs)} separate inspections in that message. One summary each follows.")
    for n, r in enumerate(recs):
        rec = normalise(r)
        key = old_key if (old_key and n == 0) else secrets.token_hex(4)
        msg = tg("sendMessage", chat_id=OWNER, text=summary_html(rec, key, " ".join(x for x in [(inbox or {}).get("note", ""), warn] if x)), parse_mode="HTML",
                 reply_markup=keyboard(("Approve and publish", f"ok:{key}"), ("Reject", f"no:{key}")))
        state["pending"][key] = {"record": rec, "raw": text, "correction": correction, "msg_id": msg["message_id"],
                                 "created": int(time.time())}
        if inbox:
            state["pending"][key].update({"from": inbox["from"], "note": inbox.get("note", "")})
        if inbox is not None:
            state.setdefault("_staged", []).append(key)
        log(f"staged {key}")
    return "staged"


def stage_list(text, L, correction, state, old_key=None, inbox=None):
    qs = [x for x in (L.get("questions") or []) if isinstance(x, dict) and x.get("q")]
    for x in qs:
        if x.get("qid") not in QIDS:
            x["qid"] = ""
    if not qs:
        if not inbox or inbox.get("correcting"):
            tg("sendMessage", chat_id=OWNER, text="That looks like a question list, but I could not read any questions from it.")
        return "not_sire"
    title = (L.get("title") or "Shared SIRE question list").strip()
    rec = {"id": slug(title)[:60] + "-" + time.strftime("%Y-%m"), "title": title, "source": L.get("source", ""),
           "added": time.strftime("%Y-%m-%d"), "questions": qs}
    key = old_key or secrets.token_hex(4)
    e = lambda v: html.escape(str(v or ""))
    lines = [f"<b>Question list</b> (not an inspection)  <code>{key}</code>"] + ([f"<i>{e(inbox['note'])}</i>"] if (inbox or {}).get("note") else []) + [
             f"<b>{e(title)}</b>", f"Source: {e(rec['source']) or '?'}",
             f"Questions: {len(qs)}   Matched to a SIRE question: {sum(1 for x in qs if x['qid'])}", ""]
    lines += [f"- {e(x.get('rank') or '?')}: {e(x['q'])} [{e(x['qid'] or 'no match')}]" for x in qs[:10]]
    if len(qs) > 10:
        lines.append(f"... and {len(qs) - 10} more")
    lines.append("\n<i>Approving adds these to the question cards and practice mode. It does not create an inspector profile.</i>")
    msg = tg("sendMessage", chat_id=OWNER, text="\n".join(lines)[:3900], parse_mode="HTML",
             reply_markup=keyboard(("Approve and publish", f"ok:{key}"), ("Reject", f"no:{key}")))
    state["pending"][key] = {"type": "list", "record": rec, "raw": text, "correction": correction, "msg_id": msg["message_id"],
                             "created": int(time.time())}
    if inbox:
        state["pending"][key].update({"from": inbox["from"], "note": inbox.get("note", "")})
    log(f"staged list {key}")
    return "staged"


def correct(key, text, state):
    p = state["pending"][key]
    try:
        tg_soft("editMessageReplyMarkup", chat_id=OWNER, message_id=p["msg_id"], reply_markup={"inline_keyboard": []})
    except Exception:
        pass
    tg("sendMessage", chat_id=OWNER, text="Applying your correction...")
    stage(p["raw"], (p.get("correction") + "\n" if p.get("correction") else "") + text, state, old_key=key)


def handle_callback(cq, state):
    data = cq.get("data", "")
    action, _, arg = data.partition(":")
    mid = cq["message"]["message_id"]
    if action in ("ok", "no"):
        p = state["pending"].pop(arg, None)
        if not p:
            return None
        if action == "no":
            tg_soft("editMessageReplyMarkup", chat_id=OWNER, message_id=mid, reply_markup={"inline_keyboard": []})
            tg("sendMessage", chat_id=OWNER, text="Rejected. Nothing was published.", reply_to_message_id=mid)
            return None
        rec = p["record"]
        if p.get("type") == "list":
            (ROOT / "field" / "lists").mkdir(exist_ok=True)
            base, n = rec["id"], 1
            while (ROOT / "field" / "lists" / f"{rec['id']}.json").exists():
                n += 1
                rec["id"] = f"{base}-{n}"
            (ROOT / "field" / "lists" / f"{rec['id']}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=True) + "\n")
        else:
            rec["id"] = free_report_id(rec["id"])
            (ROOT / "field" / "reports" / f"{rec['id']}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=True) + "\n")
        try:
            priv_put(f"raw/{rec['id']}.txt", p["raw"] + ("\n\n--- corrections ---\n" + p["correction"] if p.get("correction") else ""),
                     f"Raw report {rec['id']}")
        except Exception as e:
            log("raw save failed", type(e).__name__)
        tg_soft("editMessageReplyMarkup", chat_id=OWNER, message_id=mid, reply_markup={"inline_keyboard": []})
        tg("sendMessage", chat_id=OWNER, reply_to_message_id=mid,
           text=(f"Added the question list ({len(rec['questions'])} questions) to the SIRE page." if p.get("type") == "list" else f"Published as {rec['id']}. The SIRE page updates in a few minutes."))
        log("approved", rec["id"])
        for chat in set(p.get("from") or []):
            tg_soft("sendMessage", chat_id=chat, disable_web_page_preview=True,
                    text=f"Thank you. The SIRE report you sent is now on {SITE}\nIt will help the next crew get ready. Send more any time.")
        return "report"
    if action in ("merge", "close"):
        n = int(arg)
        pr = gh("GET", f"/repos/{REPO}/pulls/{n}", PUB)
        if pr.get("state") != "open":
            tg_soft("editMessageReplyMarkup", chat_id=OWNER, message_id=mid, reply_markup={"inline_keyboard": []})
            if not pr.get("merged_at") or action != "merge":
                tg("sendMessage", chat_id=OWNER, reply_to_message_id=mid, text="Already done: that proposal is " + ("merged." if pr.get("merged_at") else "closed."))
            return None
        try:
            if action == "merge":
                gh("PUT", f"/repos/{REPO}/pulls/{n}/merge", PUB, {"merge_method": "squash"})
            else:
                gh("PATCH", f"/repos/{REPO}/pulls/{n}", PUB, {"state": "closed"})
        except urllib.error.HTTPError as e:
            tg_soft("sendMessage", chat_id=OWNER, text=f"GitHub refused that ({e.code}). Open the pull request on GitHub to see why.")
            return None
        tg_soft("editMessageReplyMarkup", chat_id=OWNER, message_id=mid, reply_markup={"inline_keyboard": []})
        tg("sendMessage", chat_id=OWNER, reply_to_message_id=mid,
           text="Merged. The study cards on the page update within a couple of minutes." if action == "merge" else "Closed. No change made.")
        return "cards" if action == "merge" else None
    return None


# ---------------------------------------------------------------- PDF reports
def is_pdf(m):
    d = m.get("document") or {}
    return d.get("mime_type") == "application/pdf" or (d.get("file_name") or "").lower().endswith(".pdf")


def pdf_text(file_id):
    """Text of a PDF sent to the bot, or "" when it cannot be read (scanned pages, too big, locked, broken).
    Free: no DeepSeek call. Only the exception type is logged, never a path or content."""
    try:
        from pypdf import PdfReader
        info = tg("getFile", file_id=file_id)
        if info.get("file_size", 0) > PDF_MAX_BYTES:
            return ""
        url = "https://api.telegram.org/file/bot" + os.environ["TELEGRAM_BOT_TOKEN"] + "/" + info["file_path"]
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read(PDF_MAX_BYTES + 1)
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return ""
        text = "\n".join((p.extract_text() or "") for p in reader.pages[:PDF_MAX_PAGES]).strip()
        return text if len(text) >= 200 else ocr_pdf(data)     # a scan has pictures of pages and no text
    except Exception as e:
        log("pdf read failed", type(e).__name__)
        return ""


def ocr_pdf(data):
    """Scanned PDF: render the pages and read them with tesseract (free, runs on this machine, nothing is sent out)."""
    with tempfile.TemporaryDirectory() as d:
        pdf = Path(d) / "in.pdf"
        pdf.write_bytes(data)
        subprocess.run(["pdftoppm", "-r", "200", "-gray", "-l", str(OCR_MAX_PAGES), "-png", str(pdf), str(Path(d) / "p")],
                       check=True, timeout=120, capture_output=True)
        out = []
        for img in sorted(Path(d).glob("p-*.png")):
            r = subprocess.run(["tesseract", str(img), "-", "-l", "eng"], timeout=120, capture_output=True, text=True)
            out.append(r.stdout)
        text = "\n".join(out).strip()
        log("ocr", len(out), "pages")
        return text


# ---------------------------------------------------------------- inbox: reports from other seafarers
WELCOME = ("Hi! Send a SIRE 2.0 inspection report here. It helps every seafarer get ready for their next inspection.\n\n"
           "Paste it, forward it, or send it as a PDF. Anything helps: a full report, the observations, "
           "or just what the inspector asked and checked.\n\n"
           "Please remove the ship's name and crew names first.\n\n"
           f"Reports are checked once a day and added to {SITE}\nYou will get a message when yours is added.")
THANKS = ("Got it, thank you! Send more any time.\n"
          f"Reports are checked once a day. You will get a message when yours is added to {SITE}")


def today_utc():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def file_of(m):
    if m.get("photo"):
        return {"kind": "photo", "uid": m["photo"][-1]["file_unique_id"]}
    for k in ("document", "video"):
        if m.get(k):
            f = {"kind": k, "uid": m[k]["file_unique_id"]}
            if k == "document" and is_pdf(m):
                f.update(kind="pdf", fid=m[k]["file_id"], size=m[k].get("file_size", 0))
            return f
    return None


def public_message(m, state):
    """A message from anyone but the owner: queue it for the daily run. No DeepSeek here, so it costs nothing."""
    chat = m["chat"]["id"]
    reply = lambda t: tg_soft("sendMessage", chat_id=chat, text=t, disable_web_page_preview=True)
    s = state["senders"].setdefault(str(chat), {})
    if s.get("day") != today_utc():
        s.update(day=today_utc(), msgs=0, chars=0, warned=False)
    text = (m.get("text") or m.get("caption") or "").strip()
    f = file_of(m)
    if f and f["kind"] != "pdf":
        reply("Thank you. I can read text and PDF files only. Please paste the report, or send it as a PDF.")
        return
    if f and f["size"] > PDF_MAX_BYTES:
        reply("That PDF is too big for me. Please paste the text of the report instead.")
        return
    if text.startswith("/") and not f:
        reply(WELCOME)
        return
    recent = any(i["chat"] == chat and m.get("date", 0) - i["date"] <= JOIN_GAP for i in state["inbox"])
    if not f and len(text) < 40 and not recent:
        reply(WELCOME)      # "hi", "thanks", a sticker: show how it works instead of queueing it
        return
    if s["msgs"] >= SENDER_MSGS or s["chars"] + len(text) > SENDER_CHARS:
        if not s["warned"]:
            s["warned"] = True
            reply("Thank you, that is a lot for one day. Please send the rest tomorrow.")
        return
    s["msgs"] += 1
    s["chars"] += len(text)
    item = {"chat": chat, "date": m.get("date") or int(time.time()), "mid": m["message_id"]}
    if text:
        item["t"] = text
    if f:
        item["file"] = f
    state["inbox"].append(item)
    if time.time() - s.get("ack", 0) > 300:     # one thank-you per burst, not one per split part
        s["ack"] = int(time.time())
        reply(THANKS)
    log("inbox +1")


_PRIME = (1 << 61) - 1
_PERM = [(3 + 2 * random.Random(n).randrange(1 << 40), n * 7919 + 1) for n in range(32)]   # fixed: fingerprints must stay comparable across restarts


def fingerprint(text):
    """Exact fingerprint plus a 32-number MinHash of 5-word phrases. Spacing, case, links and
    "Forwarded from" lines are ignored, so forwards and re-pastes of one report look the same."""
    t = re.sub(r"(?im)^.*forwarded (from|message).*$", " ", text.lower())
    w = re.findall(r"[a-z0-9]+", re.sub(r"https?://\S+", " ", t))
    shingles = {" ".join(w[i:i + 5]) for i in range(max(1, len(w) - 4))}
    hs = [int.from_bytes(hashlib.blake2b(x.encode(), digest_size=8).digest(), "big") for x in shingles]
    mh = [min((a * h + b) % _PRIME for h in hs) & 0xFFFFFFFF for a, b in _PERM]
    return hashlib.sha256(" ".join(w).encode()).hexdigest()[:16], mh


def same_report(a, b):
    return a["fp"] == b["fp"] or sum(x == y for x, y in zip(a["mh"], b["mh"])) / len(a["mh"]) >= NEAR_DUP


def sire_like(text):
    t = text.lower()
    return sum(1 for k in SIRE_WORDS if re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", t)) >= 2   # every real report so far has 2 or more


def digest_due(state):
    now = dt.datetime.now(dt.timezone.utc)
    return now.hour >= DIGEST_HOUR and state["digest_day"] != now.strftime("%Y-%m-%d")


def seen_again(old, chat, state):
    """Someone sends a report that was read before. If its card still waits for the owner they are added to it and
    thanked on approval; otherwise they are told it is already known (so nobody waits for a message that never comes)."""
    keys = [k for k in old.get("keys", []) if k in state["pending"]]
    for k in keys:
        f = state["pending"][k].setdefault("from", [])
        if chat not in f:
            f.append(chat)
    if not keys and old.get("keys"):
        tg_soft("sendMessage", chat_id=chat, disable_web_page_preview=True,
                text=f"Thank you. I already have that report, so it is not added twice. The SIRE page is {SITE}")


def digest(state, manual=False):
    """The daily inbox run. Free steps first (join split messages, drop non-SIRE text, drop exact and near
    duplicates of anything seen before), then DeepSeek only for what is left, at most DAILY_READS a day."""
    day = today_utc()
    if not manual:
        state["digest_day"] = day
    state["senders"] = {k: v for k, v in state["senders"].items() if v.get("day") == day}
    reads = state.get("reads") if (state.get("reads") or {}).get("day") == day else {"day": day, "n": 0, "files": 0}
    state["reads"] = reads
    now = time.time()
    ready = [i for i in state["inbox"] if now - i["date"] > JOIN_GAP]   # newer ones may still be getting more parts
    if not ready:
        if manual:
            tg("sendMessage", chat_id=OWNER, text="The inbox is empty.")
        return
    state["inbox"] = [i for i in state["inbox"] if now - i["date"] <= JOIN_GAP]

    subs = []   # one submission = one person's messages sent close together
    for i in sorted(ready, key=lambda i: (i["chat"], i["date"], i["mid"])):
        if subs and subs[-1]["chat"] == i["chat"] and i["date"] - subs[-1]["last"] <= JOIN_GAP:
            subs[-1]["items"].append(i)
        else:
            subs.append({"chat": i["chat"], "items": [i]})
        subs[-1]["last"] = i["date"]

    raw, seen_sha = priv_get(SEEN_PATH)
    seen = json.loads(raw) if raw else []
    c = dict.fromkeys(["dup", "short", "staged", "not_sire", "on_page", "failed", "wait"], 0)
    texts, files = [], []   # unique new reports and files, each with every submission that sent it
    pdfs = {}   # id(item) -> text read from its PDF (kept out of the saved state)
    for sub in subs:
        parts = [i["t"] for i in sub["items"] if i.get("t")]
        for i in sub["items"]:
            if (i.get("file") or {}).get("kind") == "pdf":
                pdfs[id(i)] = pdf_text(i["file"]["fid"])
                if pdfs[id(i)]:
                    parts.append(pdfs[id(i)])
        text = "\n".join(parts)
        if len(text) >= 200 and sire_like(text):
            fp, mh = fingerprint(text)
            u = {"fp": fp, "mh": mh}
            old = next((s for s in seen if "fp" in s and same_report(s, u)), None)
            new = next((x for x in texts if same_report(x, u)), None)
            if old:
                old["n"] = old.get("n", 1) + 1
                c["dup"] += 1
                seen_again(old, sub["chat"], state)
            elif new:
                new["subs"].append(sub)
                c["dup"] += 1
            else:
                texts.append(dict(u, text=text[:MAX_REPORT], subs=[sub]))
        elif text and not any(i.get("file") and not pdfs.get(id(i)) for i in sub["items"]):
            c["short"] += 1
        for i in sub["items"]:
            if not i.get("file") or pdfs.get(id(i)):
                continue    # PDFs with readable text were read above; scanned ones go to the owner as files
            old = next((s for s in seen if s.get("uid") == i["file"]["uid"]), None)
            new = next((x for x in files if x["uid"] == i["file"]["uid"]), None)
            if old:
                old["n"] = old.get("n", 1) + 1
                c["dup"] += 1
            elif new:
                new["items"].append(i)
                c["dup"] += 1
            else:
                files.append({"uid": i["file"]["uid"], "items": [i]})

    for u in texts:
        if reads["n"] >= DAILY_READS:
            c["wait"] += 1
            state["inbox"] += [i for s in u["subs"] for i in s["items"]]   # read tomorrow, not lost
            continue
        chats = sorted({s["chat"] for s in u["subs"]})
        note = f"From the inbox: sent by {len(chats)} {'person' if len(chats) == 1 else 'people'}"
        reads["n"] += 1
        state["_staged"] = []
        try:
            outcome = stage(u["text"], "", state, inbox={"from": chats, "note": note})
        except Exception as e:
            log("inbox read failed", type(e).__name__)
            outcome = "failed"
        c[outcome] += 1
        if outcome == "failed":
            state["inbox"] += [i for s in u["subs"] for i in s["items"]]
        else:
            seen.append({"fp": u["fp"], "mh": u["mh"], "n": len(u["subs"]), "t": day, "keys": state.get("_staged") or []})
        state.pop("_staged", None)

    lines = ["<b>SIRE inbox</b>" + (" (daily)" if not manual else ""),
             f"Received: {len(ready)} messages from {len({i['chat'] for i in ready})} people"]
    for k, label in [("staged", "New reports, summaries above"), ("dup", "Duplicates dropped (no cost)"),
                     ("on_page", "Already on the page or waiting for you"), ("not_sire", "Read, but not a SIRE report"),
                     ("short", "Too short or not about SIRE (not read)"), ("failed", "Could not read, will retry tomorrow"),
                     ("wait", f"Over today's limit of {DAILY_READS} reads, will read tomorrow")]:
        if c[k]:
            lines.append(f"{label}: {c[k]}")
    send_files = files[:max(0, DAILY_FILES - reads["files"])]
    for x in files[len(send_files):]:
        state["inbox"] += x["items"]
    if send_files:
        lines.append(f"Screenshots and files below: {len(send_files)}. Paste me the text of any you want added.")
    tg("sendMessage", chat_id=OWNER, text="\n".join(lines), parse_mode="HTML")
    for x in send_files:
        i = x["items"][0]
        try:
            tg("copyMessage", chat_id=OWNER, from_chat_id=i["chat"], message_id=i["mid"])
            reads["files"] += 1
            seen.append({"uid": x["uid"], "n": len(x["items"]), "t": day})
        except Exception as e:
            log("file copy failed", type(e).__name__)
    try:
        priv_put(SEEN_PATH, json.dumps(seen[-1500:]), "Inbox fingerprints", seen_sha)
    except Exception as e:
        log("seen save failed", type(e).__name__)
    log(f"digest: {len(ready)} messages, " + ", ".join(f"{k} {v}" for k, v in c.items()))


def notify_prs(state):
    prs = gh("GET", f"/repos/{REPO}/pulls?state=open&per_page=20", PUB) or []
    for pr in prs:
        if not any(l["name"] == "hermes-cards" for l in pr.get("labels", [])) or pr["number"] in state["notified_prs"]:
            continue
        body = re.sub(r"\n{3,}", "\n\n", pr.get("body") or "")
        text = (f"<b>Hermes proposes card updates</b> (#{pr['number']})\n{html.escape(pr['title'])}\n\n"
                f"{html.escape(body[:2800])}\n\n<a href=\"{pr['html_url']}/files\">See every change</a>")
        tg("sendMessage", chat_id=OWNER, text=text, parse_mode="HTML", disable_web_page_preview=True,
           reply_markup=keyboard(("Merge", f"merge:{pr['number']}"), ("Close", f"close:{pr['number']}")))
        state["notified_prs"] = (state["notified_prs"] + [pr["number"]])[-50:]
    return False


if __name__ == "__main__":
    if "--loop" in sys.argv:
        loop()
    try:
        main()
    except Exception as e:
        log("FAILED", type(e).__name__)
        try:
            tg("sendMessage", chat_id=OWNER, text=f"SIRE bot run failed: {type(e).__name__}: {str(e)[:300]}")
        except Exception:
            pass
        raise
