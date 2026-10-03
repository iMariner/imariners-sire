# Hermes daily job: improve the SIRE study cards from new field reports

Runs on the iMariners Hermes (`hermes7.splicerun.net` = container `sr-sra4c584b4-hermes` on the Webyne VPS) as cron job
`sire-card-improvement` (id caca6c1e1d33), daily at 03:30 UTC for now, set up 2026-10-03. The prompt is stored in the
Hermes volume as /opt/data/sire_prompt.txt; the token is the `SIRE_GITHUB_TOKEN=` line in /opt/data/.env.
Change it to weekly once reports slow down. It never changes the website directly. It opens a pull request
labelled `hermes-cards`; the SIRE Telegram bot sends it to Ajit with Merge / Close buttons.

Needs, in the Hermes instance environment:
- `SIRE_GITHUB_TOKEN`: a fine-grained GitHub token for iMariner with access to `imariners-sire`
  (Contents: Read and write, Pull requests: Read and write) and `imariners-sire-raw` (Contents: Read).
- Model: DeepSeek, the same as the tracker job.

The prompt describes GitHub calls in words: Hermes blocks cron prompts with a literal curl + Authorization header.

## Cron job prompt (paste as is)

```
You maintain the SIRE 2.0 study cards on imariners.com. Work only through the GitHub REST API from short
python3 scripts (urllib), authenticating with the token in the SIRE_GITHUB_TOKEN environment variable (if it is not set in the environment, read it
from the line starting SIRE_GITHUB_TOKEN= in the file $HERMES_HOME/.env).
Never print, log or write out the token. Public repo: iMariner/imariners-sire (branch main). Private repo:
iMariner/imariners-sire-raw (read only).

1. Find new field reports: list commits on main of iMariner/imariners-sire that touched the path
   field/reports since 26 hours ago. Collect the report files added or changed by those commits
   (field/reports/*.json). If there are none, reply "No new SIRE reports today." and stop.

2. From those reports collect every question id ("qid") used in observations, questions and checks.
   Put ids with an observation first. Work on at most 12 ids this run.

3. For each id:
   a. Load the card: it is in one of cards/batch01.json ... cards/batch10.json (a JSON list; find the
      object whose "id" matches). Fields: title, plain, why, who, checks, ready, traps, say, practice
      (list of {q, a}), tip, refs.
   b. Load the official OCIMF text for that id from source/questions_raw.json in the private repo
      (fields: question, objective_and_guidance, inspection_guidance, inspector_actions,
      expected_evidence, negative_grounds). Download it once and reuse it.
   c. Load what real inspections said about it from field-data.json in the public repo:
      questions[id].asked, .obs and .checks.
   d. Decide if the card should change. Allowed changes ONLY:
      - traps: add at most one bullet when a real observation shows a way ships get caught that the
        card does not already cover. Keep traps at 6 or fewer: if already 6, replace the weakest or skip.
      - practice: add at most one {q, a} when a real inspector question asks something the card's
        practice does not cover. "q" is the real question in plain words; "a" is 2 to 4 short points
        a good answer covers.
      - tip: replace the tip only when a report gives a clearer, practical lesson.
      Every point you add must be supported by the OCIMF text for that id, or be a plain restatement
      of what the report says the inspector checked or found. Never invent rules, intervals, numbers,
      limits or references. No inspector names, ship names or crew names inside cards.
      Style: simple English for officers and ratings whose first language is not English, bullets of
      15 words or fewer, sentence case, British spelling, plain ASCII, no em dashes or spaced en dashes.
      If nothing is worth changing, leave the card alone. Fewer good changes beat many weak ones.

4. If at least one card changed:
   a. Create branch hermes/cards-<today's date> from main (add -2, -3 if it exists).
   b. For each changed batch file, write the whole updated file to that branch with the "create or
      update file contents" API (JSON with 1-space indent, ASCII only, same order of cards, every other
      card untouched). Check it still parses as JSON before writing.
   c. Open a pull request into main titled "Card updates from field reports <date>". Body: one line
      per change in the form
      <id> <field>: "<new text>"  (from report <report id>: <the observation or question it is based on>)
   d. Add the label hermes-cards to the pull request (create the label first if it does not exist).
   Never merge the pull request yourself.

5. Finish with a short summary: reports read, ids checked, cards changed, pull request link (or
   "no changes").
```
