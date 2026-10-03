# imariners-sire

Data for **SIRE 2.0 Made Easy** on [imariners.com](https://imariners.com/sire-2-0-inspection-questions/): a free prep tool that helps seafarers get ready for OCIMF SIRE 2.0 tanker inspections.

`sire-data.json` holds all 385 questions in the SIRE 2.0 Question Library. Each question has its official wording and attributes (vessel types, inspection area, core or rotational), plus iMariners plain-language study notes: what the inspector checks, what to keep ready, common findings, practice questions, and the ranks usually asked.

The page loads it through jsDelivr:
`https://cdn.jsdelivr.net/gh/iMariner/imariners-sire@main/sire-data.json`

Source: OCIMF SIRE 2.0 Question Library Part 1 and Part 2 v1.0 (January 2022), and Question Programming Attributes v2.0 (January 2023). Official documents: https://www.ocimf.org/programmes/sire-2-0

The study notes are iMariners material written for seafarers. They are not OCIMF text and do not replace a company's SMS.

## Field reports

`field/reports/*.json` holds one structured record per real SIRE 2.0 inspection: inspector, port, what was checked, questions asked per rank, observations, and depth scores per topic (`field/SCHEMA.md`). They come in through a Telegram bot (n8n with DeepSeek, using `field/extract_prompt.txt`) and are approved by hand before being committed. On every push to `field/reports/`, the GitHub Action rebuilds `field-data.json`, which the page loads.
