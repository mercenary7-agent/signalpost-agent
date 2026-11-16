from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .evidence import evidence, utc_now
from .official import accounting_obligation_assessment
from .sampling import iter_bulk


TERMINAL_STATES = {
    "complete",
    "not_applicable",
    "not_found",
    "blocked_policy",
    "blocked_robots",
    "source_error",
    "budget_exhausted",
    "submission_error",
}


def read_organisation_inputs(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    values: list[Any]
    if source.suffix == ".json":
        body = json.loads(text)
        values = body if isinstance(body, list) else body.get("organisation_numbers", [])
    elif source.suffix == ".jsonl":
        values = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        values = [line.strip() for line in text.splitlines() if line.strip()]
    records = []
    for value in values:
        org = value.get("organisation_number") if isinstance(value, dict) else value
        org = "".join(character for character in str(org or "") if character.isdigit())
        if len(org) != 9:
            raise ValueError(f"Invalid Norwegian organisation number: {value!r}")
        record = {"organisation_number": org}
        if isinstance(value, dict):
            for key in ("evaluation_split", "sample_slice"):
                if value.get(key) is not None:
                    record[key] = value[key]
        records.append(record)
    orgs = [record["organisation_number"] for record in records]
    if len(orgs) != len(set(orgs)):
        raise ValueError("Organisation-number input contains duplicates")
    return records


def read_organisation_numbers(path: str | Path) -> list[str]:
    return [record["organisation_number"] for record in read_organisation_inputs(path)]


def profiles_from_bulk(path: str | Path, organisation_numbers: Iterable[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    requested = list(organisation_numbers)
    wanted = set(requested)
    snapshot_sha256 = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    retrieved_at = utc_now()
    found: dict[str, dict[str, Any]] = {}
    scanned = 0
    for profile in iter_bulk(path):
        scanned += 1
        org = profile["organisation_number"]
        if org not in wanted:
            continue
        raw = profile.pop("raw", {})
        profile["evidence"] = {
            "registry": evidence(
                "registry",
                "available",
                "official_registry_bulk",
                "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                value=raw,
                retrieved_at=retrieved_at,
                content_sha256=snapshot_sha256,
                source_row_key=org,
            ),
            "accounting_obligation": accounting_obligation_assessment(profile),
        }
        found[org] = profile
        if len(found) == len(wanted):
            break
    missing = [org for org in requested if org not in found]
    if missing:
        raise ValueError(f"Organisation numbers absent from registry snapshot: {missing[:10]}")
    return [found[org] for org in requested], {
        "registry_snapshot_sha256": snapshot_sha256,
        "registry_rows_scanned": scanned,
        "requested": len(requested),
        "selected": len(found),
    }


def evidence_terminal_state(record: dict[str, Any] | None) -> str:
    if not record:
        return "submission_error"
    status = record.get("status")
    if status == "available":
        return "complete"
    if status == "not_applicable":
        return "not_applicable"
    if status == "not_found":
        return "not_found"
    if status == "blocked":
        note = str(record.get("note") or "").casefold()
        return "blocked_robots" if "robot" in note else "blocked_policy"
    if status == "source_error":
        return "source_error"
    return "submission_error"


def _claim_from_record(field: str, record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record or record.get("status") in {None, "not_fetched"}:
        return None
    value = record.get("value")
    if isinstance(value, dict):
        shown = {k: v for k, v in list(value.items())[:8] if not isinstance(v, (list, dict))}
        if not shown and value:
            shown = {"keys": sorted(map(str, value.keys()))[:12]}
        value = shown if shown else "see evidence value"
    return {
        "field": field,
        "value": value,
        "availability": record.get("status"),
        "source_url": record.get("source_url"),
        "source_class": record.get("source_class") or record.get("source_type"),
        "retrieved_at": record.get("retrieved_at"),
        "as_of": record.get("as_of"),
        "content_sha256": record.get("content_sha256"),
        "note": record.get("note"),
    }


def build_claims(profile: dict[str, Any]) -> list[dict[str, Any]]:
    claims = []
    for field, record in sorted((profile.get("evidence") or {}).items()):
        claim = _claim_from_record(field, record)
        if claim:
            claims.append(claim)
    return claims


def build_synthesis(profile: dict[str, Any]) -> dict[str, Any]:
    ev = profile.get("evidence") or {}
    name = profile.get("name") or ""
    org = profile.get("organisation_number") or ""
    parts = [f"{name} (organisation number {org})"]
    if profile.get("industry_label"):
        parts.append(f"is registered as {profile['industry_label']} ({profile.get('industry_code') or 'no code'}) in {profile.get('municipality') or 'unknown municipality'}")
    if profile.get("employees") is not None:
        parts.append(f"reports {profile['employees']} employees")
    else:
        parts.append("has no registered employee count")
    if profile.get("latest_submitted_accounts"):
        parts.append(f"latest annual accounts filed for {profile['latest_submitted_accounts']}")
    roles = (ev.get("roles") or {}).get("value")
    if isinstance(roles, dict):
        holders = roles.get("role_holders") or roles.get("holders") or []
        if holders:
            names = [str(h.get("name") or h.get("navn") or "") for h in holders[:3] if isinstance(h, dict)]
            names = [n for n in names if n]
            if names:
                parts.append("registered leadership includes " + ", ".join(names))
    website = ev.get("website") or {}
    wval = website.get("value") or {}
    wid = wval.get("identity_assessment") or {}
    if website.get("status") == "available" and wid.get("publishable"):
        parts.append(f"has a verified official website at {wval.get('final_url') or website.get('source_url')}")
    else:
        parts.append("no verified official website was found (marked not available, not zero)")
    news = ev.get("news") or {}
    mentions = (news.get("value") or {}).get("mentions") or []
    if mentions:
        newest = max((str(m.get("published_at") or "") for m in mentions), default="")
        parts.append(f"appears in {len(mentions)} dated news item(s) with exact-name matches" + (f", newest {newest[:10]}" if newest else ""))
    jobs = (ev.get("jobs_site") or {}).get("value") or {}
    job_posts = jobs.get("job_postings") or []
    if job_posts:
        parts.append(f"lists {len(job_posts)} job posting(s) on its own careers page")
    unknowns = []
    for field, label in (("website", "official website"), ("news", "dated news activity"), ("jobs_site", "job postings"), ("financials", "normalized annual accounts")):
        if (ev.get(field) or {}).get("status") in {"not_found", "blocked", "source_error", "not_applicable"}:
            unknowns.append(label)
    if unknowns:
        parts.append("Not found (explicitly not available): " + ", ".join(unknowns))
    return {"text": ". ".join(parts) + ".", "unknowns": unknowns}


def terminal_envelope(
    profile: dict[str, Any],
    *,
    run_id: str,
    modules: Iterable[str],
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    module_states = {}
    for module in modules:
        record = profile.get("evidence", {}).get(module)
        module_states[module] = {
            "state": evidence_terminal_state(record),
            "retry_count": int((record or {}).get("retry_count") or 0),
            "final_timestamp": (record or {}).get("retrieved_at") or completed_at,
        }
    entity_state = "submission_error" if any(item["state"] == "submission_error" for item in module_states.values()) else "complete"
    return {
        "run_id": run_id,
        "organisation_number": profile["organisation_number"],
        "state": entity_state,
        "started_at": started_at,
        "completed_at": completed_at,
        "modules": module_states,
        "claims": build_claims(profile),
        "synthesis": build_synthesis(profile),
        "profile": profile,
    }


def validate_envelopes(envelopes: list[dict[str, Any]], expected_count: int) -> dict[str, Any]:
    orgs = [item.get("organisation_number") for item in envelopes]
    invalid_states = [
        {"organisation_number": item.get("organisation_number"), "state": state.get("state")}
        for item in envelopes
        for state in item.get("modules", {}).values()
        if state.get("state") not in TERMINAL_STATES
    ]
    checks = {
        "exact_expected_count": len(envelopes) == expected_count,
        "unique_organisation_numbers": len(orgs) == len(set(orgs)),
        "all_entity_states_terminal": all(item.get("state") in TERMINAL_STATES for item in envelopes),
        "all_module_states_terminal": not invalid_states,
        "zero_silent_drops": len(envelopes) == expected_count and len(orgs) == len(set(orgs)),
    }
    return {"passed": all(checks.values()), "checks": checks, "invalid_states": invalid_states}


def profile_complete_for_modules(profile: dict[str, Any], modules: Iterable[str]) -> bool:
    records = profile.get("evidence", {})
    return all(module in records and records[module].get("status") != "not_fetched" for module in modules)
