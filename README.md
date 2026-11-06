# Congress Watch

A post-midterm tracker for the incoming U.S. Congress, built on [Telem](https://telem.ai).

It creates one **Telem Monitor** per topic (undecided Senate races, uncalled House races,
certification and recounts, leadership elections, committee chairs, cabinet changes,
lame-duck floor action, health-policy agenda). It polls them on a schedule and stores every
result, because Telem returns each result only once. Then it writes a **daily nonpartisan
briefing** in Markdown and HTML with numbered source links.

```
topics.yaml ──setup──▶ Telem Monitors (1h / 6h / 12h / 1d)
                              │ poll (or webhook)
                              ▼
                       congress_watch.db (SQLite, deduped)
                              │ brief
                              ▼
        Telem Fetch (article ledes) ──▶ briefs/2026-11-06_0700.md + .html
```

## Quick start

```bash
pip install -e .                      # Python 3.10+
export TELEM_API_KEY=tlm_...          # https://app.telem.ai (free tier includes $20 credit)

congress-watch setup                  # create the monitors from topics.yaml
congress-watch poll                   # pull new results
congress-watch brief                  # write briefs/<date>.md and .html
congress-watch status                 # monitors, expiry, unbriefed counts
```

`brief` writes a list of headlines grouped by topic. Each headline gets up to three
**complete sentences**, never a cut-off fragment. They come from the search snippet when it
has whole sentences. Otherwise the tool fetches the article through Telem and uses its opening sentences
(up to `--fetch-top` articles per section). If neither works, the headline appears on its own.

## Commands

| Command | What it does |
|---|---|
| `setup [--force] [--prune] [--webhook-url URL]` | Creates or renews monitors. It's idempotent: unchanged active monitors are left alone, changed frequencies are PATCHed, and changed queries or expired monitors are recreated. |
| `poll [--max-rounds N]` | Polls every active monitor, re-polling while Telem reports `more_runs_pending`. Marks inactive or expired monitors. |
| `brief [--fetch-top N] [--dry-run]` | Builds the briefing from everything not yet briefed. For items whose snippet has no complete sentence, it fetches the article through Telem Fetch (up to N per section) and uses its opening sentences. |
| `run` | `poll` then `brief`, the one command for cron. |
| `search "query"` | A one-off Telem search, handy for testing a new topic before adding it. |
| `teardown` | Deletes all monitors. |
| `webhook [--port 8787]` | A real-time receiver that verifies `Telem-Signature` (needs `pip install telem-sdk`). |

## Scheduling

```cron
# hourly collection, briefing at 7:00 every morning
0 * * * *  cd ~/congress-watch && congress-watch poll >> poll.log 2>&1
0 7 * * *  cd ~/congress-watch && congress-watch run  >> brief.log 2>&1
# monitors live at most 30 days: renew weekly (no-op for healthy ones)
0 6 * * 1  cd ~/congress-watch && congress-watch setup >> setup.log 2>&1
```

## Editing topics

Each entry in `topics.yaml` is one sentence describing what should trigger an alert, plus
optional `frequency`, `ttl`, and `include_domains` (up to 20 hostnames). Telem doesn't allow
editing a monitor's query, so the next `setup` replaces any changed monitor automatically.
Once the Alaska and Mississippi races are called, remove those two topics and run
`setup --prune`.

Ideas to add as the new Congress starts on Jan 3: specific bills by number, confirmation
hearings, a committee's hearing schedule, or your own state delegation.

## Troubleshooting: every topic shows `+0`

- **Monitors are new.** Each one plans its search first, then runs. `congress-watch status --remote`
  shows each monitor's agent state (`compiling` → `ready`) and next run time. `poll` also prints
  "first run still in progress" when Telem reports `first_run_pending`.
- **See exactly what Telem returned:** `congress-watch poll --raw raw/` saves each response as
  JSON. Results are read from a top-level `results` list and from
  `preprocessor_runs[*].output_payload.results`.

## Neutrality

The briefing adds no wording of its own: each item is the source's headline plus sentences
taken from its snippet or article, with a numbered link back to the source. The topic queries
are worded neutrally too. If you edit them, keep it that way.

## Tests

```bash
pip install pytest && pytest -q
```

The tests run the whole flow (setup → poll → dedupe → brief, plus monitor expiry) against a fake Telem router built from the response shapes in docs.telem.ai.

## Notes and assumptions

- Endpoints follow docs.telem.ai: `POST /v1/search`, `POST /v1/fetch`, `POST /v1/monitors`,
  `POST /v1/monitors/{id}/poll`, `PATCH` and `DELETE /v1/monitors/{id}`.
- The webhook payload is assumed to carry `results` in the same shape as a poll response.
  Check this against a real delivery before relying on webhooks; polling works either way.
