# SIRE field report format (one JSON file per inspection, in reports/)

File name: `YYYY-MM-DD-<port-slug>-<inspector-slug>.json`

```json
{
  "id": "2026-08-04-qinzhou-byeong-weon-lee",
  "date": "2026-08-04",                       // inspection date, "" if unknown
  "inspector": "Byeong Weon Lee",             // as the report gives it, title like "Capt." kept
  "port": "Qinzhou", "country": "China", "terminal": "",
  "vessel_type": "Oil",                       // Oil | Chemical | LPG | LNG | Unknown
  "hours": 8, "start": "12:00", "end": "20:00",   // null if unknown
  "schedule": [{"area": "Documents", "hours": 2}],   // order as the inspector ran it, [] if unknown
  "observations_total": 9, "high_risk": 0,    // null if unknown
  "observations": [{"text": "plain words", "qid": "2.2.1", "type": "process", "rank": "master"}],
  "positives": ["Bosun and fitter answered correctly"],
  "style": ["Goes strictly by PIQ dates, does not accept other evidence", "Repeats a question in different ways until satisfied"],
  "questions": [{"rank": "2o", "area": "Bridge", "q": "In rain, which radar, X or S band, and why?", "qid": "4.1.2", "answer": "good"}],
  "checks": [{"area": "Bridge", "item": "Contingency anchorages marked on the passage plan", "qid": "4.2.1"}],
  "depth": {"passage": 3, "nav_equip": 3},   // 0-3 per topic key in topics.json, ONLY topics the report mentions
  "tips": ["Carry a personal gas detector everywhere on deck rounds"],
  "summary": "Two or three plain sentences: how this inspection went and what this inspector cares about."
}
```

Rules:
- `rank` codes: master, co, 2o, 3o, ce, 2e, 3e, eto, bosun, ab, pumpman, fitter, oiler, cook, cadet, ratings.
- `type`: hardware | process | human. `answer`: good | partial | poor | unknown.
- Depth: 0 = mentioned as not checked; 1 = glanced at a document or asked one general question; 2 = several specific items checked or a demonstration; 3 = grilled (many specific questions or demonstrations) or an observation raised in that topic.
- No vessel names, no crew names. Crew are referred to by rank only. No personal remarks about the inspector beyond how he inspects.
- Plain ASCII, no em dashes.
