"""SIRE 2.0 field report bot. Runs in GitHub Actions every few minutes (no server, nothing on a laptop).

What it does each run:
  1. Reads new Telegram messages sent to the bot by the owner (long polling, no webhook).
  2. A pasted report goes to DeepSeek with field/extract_prompt.txt and comes back as one structured record.
     The owner gets a summary with Approve / Reject buttons. Replying to that summary with a correction
     re-reads the report with the correction applied.
  3. Approve: the record is written to field/reports/, the raw text to the private repo, field-data.json is rebuilt.
  4. Card proposals from Hermes (pull requests labelled "hermes-cards") are sent to the owner with Merge / Close.
     Merge: the PR is merged and sire-data.json is rebuilt.
State (Telegram offset, pending reports) lives in the private repo, so no report text ever lands in public logs.

Environment: TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID, DEEPSEEK_API_KEY, DEEPSEEK_MODEL (optional),
PRIVATE_REPO_TOKEN (contents read/write on the private repo), GITHUB_TOKEN (this repo), REPO, RAW_REPO.
"""
import base64, datetime as dt, html, json, os, re, secrets, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = os.environ.get("REPO", "iMariner/imariners-sire")
RAW_REPO = os.environ.get("RAW_REPO", "iMariner/imariners-sire-raw")
TG = "https://api.telegram.org/bot" + os.environ["TELEGRAM_BOT_TOKEN"] + "/"
OWNER = int(os.environ["TELEGRAM_OWNER_ID"])
MODEL = os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat"
STATE_PATH = "state/bot.json"
SPLIT_LEN = 3500          # Telegram cuts long pastes into ~4096 char parts; a part this long probably continues
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


def tg(method, **params):
    return http("POST", TG + method, params, timeout=40).get("result")


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


def summary_html(rec, key):
    e = lambda s: html.escape(str(s if s is not None else ""))
    missing = [k for k in ("inspector", "port", "date") if not rec.get(k)]
    place = ", ".join(x for x in [rec.get("port"), rec.get("terminal"), rec.get("country")] if x) or "?"
    lines = [f"<b>SIRE report</b>  <code>{key}</code>",
             f"Inspector: <b>{e(rec.get('inspector') or '?')}</b>",
             f"Port: {e(place)}   Date: {e(rec.get('date') or '?')}   Ship: {e(rec.get('vessel_type'))}",
             f"Observations: {e(rec.get('observations_total') if rec.get('observations_total') is not None else len(rec['observations']))}"
             f"   Questions: {len(rec['questions'])}   Checks: {len(rec['checks'])}",
             "Depth: " + (", ".join(f"{k} {v}" for k, v in sorted(rec["depth"].items(), key=lambda kv: -kv[1])) or "none")]
    if rec["observations"]:
        lines.append("\n<b>Observations</b>")
        lines += [f"- {e(o.get('text'))} [{e(o.get('qid') or 'no match')}]" for o in rec["observations"][:8]]
    if rec["questions"]:
        lines.append("\n<b>Questions</b>")
        lines += [f"- {e(q.get('rank'))}: {e(q.get('q'))} [{e(q.get('qid') or 'no match')}]" for q in rec["questions"][:8]]
        if len(rec["questions"]) > 8:
            lines.append(f"... and {len(rec['questions']) - 8} more")
    if rec["style"]:
        lines.append("\n<b>Style</b>")
        lines += [f"- {e(s)}" for s in rec["style"][:5]]
    lines.append(f"\n{e(rec.get('summary'))}")
    if missing:
        lines.append(f"\n<i>Missing: {', '.join(missing)}. Reply to this message to add them.</i>")
    lines.append("<i>Reply to this message with any correction before approving.</i>")
    out = "\n".join(lines)
    return out if len(out) < 3900 else out[:3850] + "\n... (cut)"


def keyboard(*buttons):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}


# ---------------------------------------------------------------- main
def main():
    raw_state, state_sha = priv_get(STATE_PATH)
    state = json.loads(raw_state) if raw_state else {}
    state.setdefault("offset", 0)
    state.setdefault("pending", {})
    state.setdefault("buffer", [])
    state.setdefault("notified_prs", [])
    changed_reports, changed_cards, purge = False, False, False

    updates = tg("getUpdates", offset=state["offset"], timeout=0, allowed_updates=["message", "callback_query"]) or []
    log(f"updates: {len(updates)}")
    texts = []
    for u in updates:
        state["offset"] = u["update_id"] + 1
        if "callback_query" in u:
            cq = u["callback_query"]
            if cq["from"]["id"] != OWNER:
                continue
            handled = handle_callback(cq, state)
            changed_reports |= handled == "report"
            changed_cards |= handled == "cards"
            continue
        m = u.get("message") or {}
        fid = (m.get("from") or {}).get("id")
        if fid != OWNER or m.get("chat", {}).get("type") != "private":
            # diagnostics without printing ids (logs are public)
            bot_id = int(os.environ["TELEGRAM_BOT_TOKEN"].split(":")[0]) if ":" in os.environ["TELEGRAM_BOT_TOKEN"] else 0
            log(f"ignored message: owner_match={fid == OWNER} chat={m.get('chat', {}).get('type')} "
                f"sender_digits={len(str(fid))} owner_digits={len(str(OWNER))} owner_is_bot_id={OWNER == bot_id}")
            continue
        text = (m.get("text") or m.get("caption") or "").strip()
        if not text:
            if m.get("document") or m.get("photo"):
                tg("sendMessage", chat_id=OWNER, text="Send the report as text (paste it). Files and photos are not read yet.")
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
        if cur and len(cur[-1]["t"]) >= SPLIT_LEN and part["date"] - cur[-1]["date"] <= 120:
            cur.append(part)
        else:
            if cur:
                reports.append(cur)
            cur = [part]
    if cur:
        if len(cur[-1]["t"]) >= SPLIT_LEN and time.time() - cur[-1]["date"] < 90:
            state["buffer"] = cur        # more parts may still be on the way
        else:
            reports.append(cur)
    for parts in reports:
        new_report("\n".join(p["t"] for p in parts), state)

    changed_cards |= notify_prs(state)

    # keep at most 30 pending reports
    if len(state["pending"]) > 30:
        for k in sorted(state["pending"], key=lambda k: state["pending"][k]["created"])[:-30]:
            del state["pending"][k]

    if changed_cards:
        run("git", "pull", "--rebase", "--autostash", "-q")
        run(sys.executable, "scripts/build_data.py")
    if changed_reports or changed_cards:
        run(sys.executable, "field/scripts/aggregate.py")
        run("git", "add", "-A", "field", "field-data.json", "sire-data.json", "cards")
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode:
            run("git", "commit", "-q", "-m", "Bot: " + ("new field report" if changed_reports else "") +
                (" and " if changed_reports and changed_cards else "") + ("card update merged" if changed_cards else ""))
            run("git", "pull", "--rebase", "--autostash", "-q")
            run("git", "push", "-q")
            purge = True
    if purge:
        for f in ("field-data.json", "sire-data.json"):
            try:
                http("GET", f"https://purge.jsdelivr.net/gh/{REPO}@main/{f}", timeout=30)
            except Exception as e:
                log("purge failed", f, type(e).__name__)

    priv_put(STATE_PATH, json.dumps(state, indent=1), "Bot state", state_sha)
    log(f"done: pending {len(state['pending'])}, buffered {len(state['buffer'])}")


def run(*cmd):
    subprocess.run(cmd, check=True, cwd=ROOT)


def command(text, state):
    cmd = text.split()[0].split("@")[0].lower()
    if cmd in ("/start", "/help"):
        msg = ("Paste or forward a SIRE 2.0 inspection report as text. I read it, show you a summary, and publish it "
               "to imariners.com when you tap Approve.\nReply to a summary to correct it (for example: port is Houston).\n"
               "/status  what is in the database\n/pending  reports waiting for you")
    elif cmd == "/status":
        f = json.loads((ROOT / "field-data.json").read_text()) if (ROOT / "field-data.json").exists() else {}
        msg = (f"Reports: {f.get('n_reports', 0)}\nInspectors: {f.get('n_inspectors', 0)}\nReal questions: {f.get('n_questions', 0)}\n"
               f"Latest: {f.get('updated') or '-'}\nWaiting for you: {len(state['pending'])}")
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


def stage(text, correction, state, old_key=None):
    try:
        res = extract(text, correction)
    except Exception as e:
        log("extract failed", type(e).__name__)
        tg("sendMessage", chat_id=OWNER, text=f"Could not read it ({type(e).__name__}: {str(e)[:200]}). Send it again later.")
        return
    if not res.get("is_sire_report"):
        tg("sendMessage", chat_id=OWNER, text=f"Not a SIRE report: {res.get('reason', '')}\nTopic: {res.get('topic_hint', '')}")
        return
    recs = res.get("reports") or ([res["report"]] if res.get("report") else [])
    recs = [r for r in recs if isinstance(r, dict)]
    if not recs:
        tg("sendMessage", chat_id=OWNER, text="I could not find an inspection in that text. Send it again with more detail.")
        return
    if old_key and len(recs) > 1:
        # a correction to one summary: keep only that inspection
        want = slug((state["pending"].get(old_key) or {}).get("record", {}).get("inspector"))
        recs = [r for r in recs if slug(r.get("inspector")) == want][:1] or recs[:1]
    if len(recs) > 1 and not old_key:
        tg("sendMessage", chat_id=OWNER, text=f"I found {len(recs)} separate inspections in that message. One summary each follows.")
    for n, r in enumerate(recs):
        rec = normalise(r)
        key = old_key if (old_key and n == 0) else secrets.token_hex(4)
        msg = tg("sendMessage", chat_id=OWNER, text=summary_html(rec, key), parse_mode="HTML",
                 reply_markup=keyboard(("Approve and publish", f"ok:{key}"), ("Reject", f"no:{key}")))
        state["pending"][key] = {"record": rec, "raw": text, "correction": correction, "msg_id": msg["message_id"],
                                 "created": int(time.time())}
        log(f"staged {key}")


def correct(key, text, state):
    p = state["pending"][key]
    try:
        tg("editMessageReplyMarkup", chat_id=OWNER, message_id=p["msg_id"], reply_markup={"inline_keyboard": []})
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
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Expired. Send the report again.")
            return None
        if action == "no":
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Rejected")
            tg("editMessageReplyMarkup", chat_id=OWNER, message_id=mid, reply_markup={"inline_keyboard": []})
            tg("sendMessage", chat_id=OWNER, text="Rejected. Nothing was published.", reply_to_message_id=mid)
            return None
        rec = p["record"]
        rec["id"] = free_report_id(rec["id"])
        (ROOT / "field" / "reports" / f"{rec['id']}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=True) + "\n")
        try:
            priv_put(f"raw/{rec['id']}.txt", p["raw"] + ("\n\n--- corrections ---\n" + p["correction"] if p.get("correction") else ""),
                     f"Raw report {rec['id']}")
        except Exception as e:
            log("raw save failed", type(e).__name__)
        tg("answerCallbackQuery", callback_query_id=cq["id"], text="Published")
        tg("editMessageReplyMarkup", chat_id=OWNER, message_id=mid, reply_markup={"inline_keyboard": []})
        tg("sendMessage", chat_id=OWNER, reply_to_message_id=mid,
           text=f"Published as {rec['id']}. The SIRE page updates in a few minutes.")
        log("approved", rec["id"])
        return "report"
    if action in ("merge", "close"):
        n = int(arg)
        try:
            if action == "merge":
                gh("PUT", f"/repos/{REPO}/pulls/{n}/merge", PUB, {"merge_method": "squash"})
            else:
                gh("PATCH", f"/repos/{REPO}/pulls/{n}", PUB, {"state": "closed"})
        except urllib.error.HTTPError as e:
            tg("answerCallbackQuery", callback_query_id=cq["id"], text=f"GitHub said {e.code}")
            return None
        tg("answerCallbackQuery", callback_query_id=cq["id"], text="Merged" if action == "merge" else "Closed")
        tg("editMessageReplyMarkup", chat_id=OWNER, message_id=mid, reply_markup={"inline_keyboard": []})
        tg("sendMessage", chat_id=OWNER, reply_to_message_id=mid,
           text="Merged. The study cards update in a few minutes." if action == "merge" else "Closed. No change made.")
        return "cards" if action == "merge" else None
    return None


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
    try:
        main()
    except Exception as e:
        log("FAILED", type(e).__name__)
        try:
            tg("sendMessage", chat_id=OWNER, text=f"SIRE bot run failed: {type(e).__name__}: {str(e)[:300]}")
        except Exception:
            pass
        raise
