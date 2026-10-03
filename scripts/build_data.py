"""Build sire-data.json (the data imariners.com/sire-2-0/ loads) from:
  cards/meta.json      question attributes from the OCIMF Question Library and Programming Attributes
  cards/batch*.json    iMariners plain-language study cards (edit these)
Run from the repo root: python3 scripts/build_data.py
"""
import json, re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = re.compile("[\u2014\u2013]")
RANKS = {"master", "co", "2o", "3o", "ce", "2e", "3e", "eto", "ratings", "deck", "engine", "galley"}
RANK_FIX = {"4e": "3e", "c/o": "co", "2/o": "2o", "3/o": "3o", "c/e": "ce", "2/e": "2e", "3/e": "3e", "bosun": "ratings", "pumpman": "ratings", "ab": "ratings"}


def clean(s):
    s = DASH.sub(",", s or "") if " – " in (s or "") or "—" in (s or "") else (s or "")
    return s.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"').strip()


def ranks(a):
    out = []
    for r in a or []:
        r = RANK_FIX.get(str(r).lower().strip(), str(r).lower().strip())
        if r in RANKS and r not in out:
            out.append(r)
    return out





ENGINE_AREAS = {"Engine Room", "Engine Control Room", "Steering Gear", "Chief Engineer's Office"}


DECK_AREAS = {"Main Deck", "Forecastle", "Mooring Decks", "Aft Mooring Deck", "Cargo Manifold", "Pumproom", "Lifeboat deck",
              "Exterior Decks", "Compressor Room", "Bow Loading Area", "Cargo Control Room", "Bridge"}
CREW_WIDE = {"2.7.1", "2.7.2", "3.5.1", "3.5.2", "5.3.4", "5.4.8", "5.7.2", "5.10.7", "8.2.6", "8.4.5"}
GALLEY_TOO = {"5.2.8", "5.8.6", "6.1.4"}


def split_ratings(ranks_, r):
    """Cards were written with one generic 'ratings' code. Turn it into deck ratings, engine ratings and/or
    galley staff: specific ROVIQ interview tags first; a general 'Interview - Rating' means all ratings;
    otherwise the inspection areas decide."""
    if "ratings" not in ranks_ and r["id"] not in GALLEY_TOO:
        return ranks_
    loc = set(r["roviq"] or [])
    g = []
    if "Interview - Deck Rating" in loc:
        g.append("deck")
    if "Interview - Engine Rating" in loc:
        g.append("engine")
    if "Interview - Galley Rating" in loc or r["id"] in GALLEY_TOO:
        g.append("galley")
    if "Interview - Rating" in loc:
        g += ["deck", "engine"]
    if r["id"] in CREW_WIDE:
        g += ["deck", "engine", "galley"]
    if not g or g == ["galley"] and "ratings" in ranks_ and r["id"] not in GALLEY_TOO:
        if loc & ENGINE_AREAS or r["chapter"] == 10:
            g.append("engine")
        if loc & DECK_AREAS or not loc & ENGINE_AREAS:
            if r["chapter"] != 10:
                g.append("deck")
    if "ratings" not in ranks_:
        ranks_ = ranks_ + ["ratings"] if r["id"] in GALLEY_TOO else ranks_
        g = [x for x in g if x == "galley"]
    out = []
    for x in ranks_:
        for y in (g if x == "ratings" else [x]):
            if y not in out:
                out.append(y)
    return out


# Cross-check (2026-10-04) against INTERTANKO's indicative tagged ranks (Seafarers' Practical Guide to SIRE 2.0, V1 2023):
# questions where ratings are also interviewed. Used only to ADD "may also be asked" groups, never to change leads.
ALSO_RATINGS = {
    "deck_engine": "5.1.6 5.1.14 5.1.15 5.2.1 5.2.2 5.2.15 5.2.16 5.3.1 5.3.2 5.3.3 5.3.4 5.4.7 5.4.8 5.5.1 5.5.2 5.5.3 5.5.4 "
                   "5.7.2 5.7.3 5.7.4 5.7.5 5.7.6 5.7.7 5.7.8 5.8.2 5.8.3 5.8.4 5.8.5 5.8.6 5.8.7 5.9.1 5.10.7 5.12.1 5.12.2 "
                   "3.5.2 4.4.6 6.4.2 7.2.2 8.3.8 8.4.5 8.6.2 8.6.13 8.2.6 9.4.1 9.4.2",
    "all_crew": "3.1.1 3.1.2 3.4.1 3.4.2 3.5.1 2.7.1 2.7.2",
    "deck": "5.10.1 5.10.2 5.10.3 6.4.1 8.3.5 8.3.11 8.6.8 9.1.1 9.1.3 9.5.2 9.5.3",
    "engine": "5.3.5",
    "galley": "5.2.8",
}
ALSO_GROUPS = {}
for k, v in ALSO_RATINGS.items():
    g = {"deck_engine": ["deck", "engine"], "all_crew": ["deck", "engine", "galley"]}.get(k, [k])
    for qid in v.split():
        ALSO_GROUPS.setdefault(qid, [])
        ALSO_GROUPS[qid] += [x for x in g if x not in ALSO_GROUPS[qid]]


def build():
    raw = {r["id"]: r for r in json.loads((ROOT / "cards" / "meta.json").read_text())}
    plain = {}
    for f in sorted((ROOT / "cards").glob("batch*.json")):
        for c in json.loads(f.read_text()):
            plain[c["id"]] = c
    missing = [i for i in raw if i not in plain]
    if missing:
        print(f"WARNING: {len(missing)} questions have no plain card yet (first: {missing[:5]}). They are left out.")
    qs, problems = [], []
    for i, r in raw.items():
        c = plain.get(i)
        if not c:
            continue
        lead, also = ranks(c.get("who", {}).get("lead")), ranks(c.get("who", {}).get("also"))
        if any(l.startswith("Interview") and "Rating" in l for l in r["roviq"]) and "ratings" not in lead:
            lead.append("ratings")  # the ROVIQ says the inspector interviews a rating for this one
        if "Interview - Electrician / ETO" in r["roviq"] and "eto" not in lead:
            lead.append("eto")
        if set(r["roviq"] or []) <= {"Documentation", "Pre-board"} and r["roviq"]:
            # document questions are reviewed with the Master in his office at the start of the inspection
            juniors = [x for x in lead if x not in ("master", "co", "ce")]
            lead = ["master"] + [x for x in lead if x in ("co", "ce")]
            also = juniors + also
        also = [x for x in also if x not in lead]
        lead, also = split_ratings(lead, r), split_ratings(also, r)
        also += [x for x in ALSO_GROUPS.get(i, []) if x not in lead and x not in also]
        also = [x for x in also if x not in lead]
        if not lead:
            problems.append(f"{i}: no lead rank")
        q = dict(
            i=i, ch=r["chapter"], sec=r["section"], ty=r["type"],
            v=[k for k, x in r["vessel"].items() if x], loc=r["roviq"] or ["Anywhere"],
            r=dict(h=int(r["response"]["hardware"] not in (None, "None")), p=int(r["response"]["process"] not in (None, "None")),
                   u=int(r["response"]["human"] not in (None, "None"))),
            oq=r["question"], t=clean(c.get("title") or r["short"]), p=clean(c.get("plain")), w=clean(c.get("why")),
            who=dict(lead=lead, also=also), c=[clean(x) for x in c.get("checks", [])], rd=[clean(x) for x in c.get("ready", [])],
            x=[clean(x) for x in c.get("traps", [])], s=clean(c.get("say")),
            pr=[dict(q=clean(p["q"]), a=[clean(x) for x in p.get("a", [])]) for p in c.get("practice", []) if p.get("q")],
            tip=clean(c.get("tip")), ref=[clean(x) for x in c.get("refs", [])][:4],
        )
        qs.append(q)
    qs.sort(key=lambda q: [int(p) for p in q["i"].split(".")])
    data = {"v": "QL 1.0 (2022-01) + QPA 2.0 (2023-01)", "q": qs}
    js = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
    (ROOT / "sire-data.json").write_text(js)
    for p in problems:
        print("  ", p)
    print(f"sire-data.json: {len(qs)} questions, {len(js)//1024} KB")
    return qs, raw



if __name__ == "__main__":
    build()
