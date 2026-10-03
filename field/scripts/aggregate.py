"""Build field-data.json from field/reports/*.json (SIRE 2.0 field reports).

Run from the repo root:  python3 field/scripts/aggregate.py
Output: field-data.json (loaded by imariners.com/sire-2-0-inspection-questions/)
"""
import json, re, glob, statistics
from pathlib import Path
from collections import Counter, defaultdict

ROOT = Path(__file__).resolve().parents[2]
FIELD = ROOT / "field"
TOPICS = json.loads((FIELD / "topics.json").read_text())
TKEYS = [t["k"] for t in TOPICS]


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def inspector_key(name):
    n = re.sub(r"^(capt|captain|mr|mrs|ms|dr|c/e|ch\.?\s*eng)\.?\s+", "", (name or "").strip(), flags=re.I)
    return slug(n)


def topic_of(qid):
    best = None
    for t in TOPICS:
        for p in t["prefix"]:
            if qid.startswith(p) and (best is None or len(p) > len(best[1])):
                best = (t["k"], p)
    return best[0] if best else None


DECK = {"bosun", "ab", "os", "pumpman", "deck"}
ENGINE = {"fitter", "oiler", "wiper", "motorman", "engine"}
GALLEY = {"cook", "steward", "messman", "galley"}
ENGINE_AREAS = {"Engine Room", "Engine Control Room", "Steering Gear", "Chief Engineer's Office"}


def rank_group(rank, area=""):
    """Rank filter group used on the page: officers keep their own code, ratings go to deck, engine or galley."""
    r = (rank or "").lower()
    if r in DECK:
        return "deck"
    if r in ENGINE:
        return "engine"
    if r in GALLEY:
        return "galley"
    if r == "ratings":
        return "engine" if area in ENGINE_AREAS else ("galley" if "Galley" in (area or "") else "deck")
    return r


def main():
    reports = []
    for f in sorted(glob.glob(str(FIELD / "reports" / "*.json"))):
        try:
            reports.append(json.loads(Path(f).read_text()))
        except Exception as e:
            print("skip", f, e)
    reports.sort(key=lambda r: r.get("date") or "0000", reverse=True)

    insp = defaultdict(list)
    for r in reports:
        if r.get("inspector"):
            insp[inspector_key(r["inspector"])].append(r)

    inspectors = []
    for key, rs in insp.items():
        depth = {}
        for k in TKEYS:
            vals = [r["depth"][k] for r in rs if k in (r.get("depth") or {})]
            if vals:
                depth[k] = {"avg": round(statistics.mean(vals), 2), "n": len(vals)}
        ranks = Counter(q.get("rank") for r in rs for q in r.get("questions", []) if q.get("rank"))
        obs = [r["observations_total"] for r in rs if isinstance(r.get("observations_total"), int)]
        hrs = [r["hours"] for r in rs if isinstance(r.get("hours"), (int, float))]
        names = Counter(r["inspector"] for r in rs)
        inspectors.append({
            "key": key, "name": names.most_common(1)[0][0], "reports": len(rs),
            "ports": sorted({", ".join(x for x in [r.get("port") or r.get("terminal"), r.get("country")] if x) for r in rs} - {""}),
            "last": max((r.get("date") or "") for r in rs),
            "avg_obs": round(statistics.mean(obs), 1) if obs else None,
            "avg_hours": round(statistics.mean(hrs), 1) if hrs else None,
            "depth": depth, "ranks": dict(ranks.most_common()),
            "style": list(dict.fromkeys(s for r in rs for s in r.get("style", [])))[:10],
            "questions": [dict(q, date=r.get("date", "")) for r in rs for q in r.get("questions", [])][:60],
            "observations": [dict(o, date=r.get("date", "")) for r in rs for o in r.get("observations", [])][:40],
            "report_ids": [r["id"] for r in rs],
        })
    inspectors.sort(key=lambda i: (-i["reports"], i["name"]))

    byq = defaultdict(lambda: {"asked": [], "checks": [], "obs": [], "reports": set(), "inspectors": set()})
    for r in reports:
        ik = inspector_key(r.get("inspector"))
        for kind, arr in (("asked", "questions"), ("checks", "checks"), ("obs", "observations")):
            for x in r.get(arr, []):
                q = x.get("qid")
                if not q:
                    continue
                e = byq[q]
                item = {"t": x.get("q") or x.get("item") or x.get("text"), "r": x.get("rank", ""), "g": rank_group(x.get("rank"), x.get("area", "")), "rid": r["id"], "ins": r.get("inspector", ""),
                        "port": r.get("port") or r.get("terminal", "")}
                if kind == "obs":
                    item["type"] = x.get("type", "")
                e[kind].append(item)
                e["reports"].add(r["id"])
                if ik:
                    e["inspectors"].add(ik)
    questions = {q: {"n": len(e["reports"]), "ni": len(e["inspectors"]), "asked": e["asked"][:12], "checks": e["checks"][:12], "obs": e["obs"][:12]}
                 for q, e in byq.items()}

    hot = sorted(questions.items(), key=lambda kv: (-kv[1]["n"], -len(kv[1]["obs"]), kv[0]))[:20]
    topic_hits = Counter()
    for r in reports:
        for k, v in (r.get("depth") or {}).items():
            if v and v >= 2:
                topic_hits[k] += 1
    rank_hits = Counter(q.get("rank") for r in reports for q in r.get("questions", []) if q.get("rank"))

    out = {
        "updated": max([r.get("date") or "" for r in reports] or [""]),
        "topics": TOPICS, "n_reports": len(reports), "n_inspectors": len(inspectors),
        "n_questions": sum(len(r.get("questions", [])) for r in reports),
        "n_obs": sum(len(r.get("observations", [])) for r in reports),
        "inspectors": inspectors,
        "reports": [{k: r.get(k) for k in ("id", "date", "inspector", "port", "country", "terminal", "vessel_type", "hours", "start", "end",
                                           "schedule", "observations_total", "high_risk", "observations", "positives", "tips", "summary", "depth")}
                    | {"inspector_key": inspector_key(r.get("inspector")), "n_questions": len(r.get("questions", [])), "n_checks": len(r.get("checks", []))}
                    for r in reports],
        "questions": questions,
        "hot": [{"qid": q, "n": v["n"], "obs": len(v["obs"])} for q, v in hot],
        "topic_hits": dict(topic_hits), "rank_hits": dict(rank_hits.most_common()),
    }
    js = json.dumps(out, ensure_ascii=True, separators=(",", ":"))
    (ROOT / "field-data.json").write_text(js)
    print(f"{len(reports)} reports, {len(inspectors)} inspectors, {len(questions)} question ids, {len(js)//1024} KB")


if __name__ == "__main__":
    main()
