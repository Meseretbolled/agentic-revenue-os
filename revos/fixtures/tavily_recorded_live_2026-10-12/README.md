# Recorded from the LIVE Tavily API

These are REAL responses, recorded on 2026-10-12 by `revos enrich` (live) for two public companies, Orangetheory
Fitness (orangetheory.com) and Hawke Media (hawkemedia.com). They are NOT synthetic (those are in
`tests/fixtures/tavily/`). Each file is one request: the request, Tavily's answer, the fetch time and the credits.
`revos enrich ... --replay` and `ENRICHMENT_CACHE=replay` read them, so the demo works with no network and no key.
The text is public web content returned by Tavily; the repo is private. Nothing was sent to either company.

| file | op | request | fetched at (UTC) | credits |
|---|---|---|---|---|
| 22c7b56c6c3b65a538dbe5ca.json | search | `{"max_results": 5, "query": "Orangetheory Fitness orangetheory.com"}` | 2026-10-12T13:48:10+00:00 | 1 |
| 6ef9fa7323cf740c8c22289f.json | search | `{"max_results": 5, "query": "Hawke Media hawkemedia.com"}` | 2026-10-12T13:48:40+00:00 | 1 |
| 8e4ddb83fb5d10c2c6c23ff1.json | extract | `{"urls": ["https://www.orangetheory.com/en-us"]}` | 2026-10-12T13:48:11+00:00 | 0 |
| dbc5c79220acfa0590764a72.json | extract | `{"urls": ["https://hawkemedia.com"]}` | 2026-10-12T13:48:40+00:00 | 0 |
