from __future__ import annotations

import hashlib
import re
from typing import Any

from .evidence import evidence, utc_now

JOB_URL_TERMS = ("karriere", "jobb", "stilling", "ledige", "career", "jobs", "vacanc", "soker", "soknad")
JOB_TEXT_TERMS = ("ledige stillinger", "vi søker", "vi soker", "join our team", "ledige stilling", "søk nå", "sok nå")
MAX_JOBS = 8


def _jobs_from_site(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Job-posting observations from the exact-gated company site only.

    Identity is inherited from the website identity gate (the site printed the
    entity's own organisation number), so every row is exact-entity by
    construction. Absence of a jobs page is not a negative claim.
    """
    website = (profile.get("evidence") or {}).get("website") or {}
    value = website.get("value") or {}
    identity = value.get("identity_assessment") or {}
    if website.get("status") != "available" or not identity.get("publishable"):
        return []
    org = str(profile["organisation_number"])
    name = str(profile.get("name") or "")
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in value.get("pages") or []:
        url = str(page.get("url") or "")
        title = str(page.get("title") or "")
        text = str(page.get("main_text_excerpt") or "")
        digest = str(page.get("content_sha256") or website.get("content_sha256") or "")
        hay_url = url.casefold()
        hay_text = text.casefold()
        is_job_page = any(term in hay_url for term in JOB_URL_TERMS) or any(term in hay_text for term in JOB_TEXT_TERMS)
        if not is_job_page:
            continue
        # Extract individual vacancy-looking lines when possible, else the page itself.
        lines = [ln.strip() for ln in text.splitlines() if 12 <= len(ln.strip()) <= 160]
        vacancy_lines = [
            ln for ln in lines
            if any(term in ln.casefold() for term in ("stilling", "jobb", "karriere", "søk", "soker", "vacanc", "position"))
            and not any(bad in ln.casefold() for bad in ("personvern", "privacy", "cookie", "vilkår", "terms"))
        ]
        candidates = vacancy_lines[:MAX_JOBS] or [title or url]
        for line in candidates:
            if len(jobs) >= MAX_JOBS:
                break
            key = line.casefold()
            if key in seen:
                continue
            seen.add(key)
            jobs.append({
                "id": "jobsite-" + hashlib.sha256(f"{org}|{url}|{line}".encode("utf-8")).hexdigest()[:24],
                "organisation_number": org,
                "platform": "company_site",
                "signal_type": "job_posting",
                "title": line[:160],
                "source_url": url,
                "retrieved_at": website.get("retrieved_at") or utc_now(),
                "published_at": None,
                "content_sha256": digest if len(digest) == 64 else hashlib.sha256(url.encode("utf-8")).hexdigest(),
                "exact_entity": True,
                "identity_proof": [
                    {"type": "website_identity_gate", "status": identity.get("status"), "score": identity.get("score")},
                    {"type": "exact_legal_name", "value": name},
                ],
                "acquisition_mode": "permitted_public_page",
                "rights_status": "approved",
                "source_class": "company_site",
                "evidence_span": line[:300],
                "metrics": {"source_page_title": title[:160]},
                "strategy": "company_site_jobs",
            })
        if len(jobs) >= MAX_JOBS:
            break
    return jobs


def fetch_jobs_site(profile: dict[str, Any]) -> tuple[dict[str, Any], dict]:
    jobs = _jobs_from_site(profile)
    record = evidence(
        "jobs_site",
        "available" if jobs else "not_found",
        "company_owned_careers_page",
        (profile.get("evidence", {}).get("website", {}).get("value") or {}).get("final_url")
        or (profile.get("evidence", {}).get("website", {}).get("source_url"))
        or "",
        value={"job_postings": jobs, "source": "exact-gated company website careers/jobs pages"},
        note=f"{len(jobs)} job posting(s) from the verified company site" if jobs else "No careers/jobs page found on the verified company site",
    )
    metric = {"requests": 0, "bytes": 0, "latencies_ms": []}
    return record, metric
