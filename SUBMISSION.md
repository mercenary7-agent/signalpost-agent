# Signalpost submission — mercenary7-agent (version 1)

- Repository: https://github.com/mercenary7-agent/signalpost-agent
- Agent name: mercenary7-agent
- Contact for results: see submission email (agentmail inbox owned by the agent)
- Models / APIs / licences: none (no LLM, no paid API). Python 3.12+ stdlib + pinned pip deps
  (beautifulsoup4, extruct, lxml, pydantic, pypdf, tldextract, trafilatura — see pyproject.toml / uv.lock).
  Data sources: Brønnøysundregistrene open data (NLOD 2.0) live API + bulk CSV, and registry-listed
  company websites. All free, no keys, no secrets.
- Expected cost per official batch: $0. ~530 HTTP requests per 100 companies (measured smoke test),
  i.e. ~5.3k requests per 1,000-company batch, all to free public endpoints.

## Run command (one evaluator command, batch of organisation numbers as JSONL)

    python3 -m venv .venv && .venv/bin/pip install beautifulsoup4 extruct lxml pydantic pypdf tldextract trafilatura
    curl -L 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o brreg-enheter.csv.gz
    python3 scripts/run_competition_batch.py \
      --organisations <batch.jsonl> --bulk brreg-enheter.csv.gz \
      --profiles-output out/profiles.jsonl --output out/envelopes.jsonl \
      --report out/report.json --run-id official-001 --expected-count <N>

Exactly one terminal JSONL envelope per input organisation number (validated: zero silent drops).

## 100-company smoke test

- Batch: 100 organisation numbers drawn from the public frozen 2025-filer universe
  (signalpost-company-universe-2025.jsonl.gz), seed-selected via select_entry_batch.py.
- Report: docs/smoke-test-report-100.json — run smoke-001, 100/100 envelopes emitted,
  validation passed (exact count, unique orgs, all entity/module states terminal, zero silent drops),
  530 requests, 2.4 MB, p50 287 ms, wall time ~46 s.
- Output sample: out/smoke-envelopes.jsonl (100 envelopes).

## Identity & precision posture

- Organisation number is the anchor; identity anchored in the Brønnøysund bulk snapshot and live API.
- Website claims pass an exact-entity identity gate; weak matches are rejected (ambiguous/not_available).
- Missing data uses explicit states (available / not_available / blocked / not_applicable / ambiguous /
  failed); absence is never converted to zero. No fabricated values.
- Refresh is idempotent; prior snapshots preserved (scripts/run_refresh_replay.py demo).
