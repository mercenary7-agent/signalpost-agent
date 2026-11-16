from __future__ import annotations

import hashlib
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .discovery import GENERIC_NAME_TOKENS, build_company_search_query, choose_search_candidate

DDG_HTML_ENDPOINT = "https://html.duckduckgo.com/html/"
DDG_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Polite global rate limit: one DDG request at a time, >= MIN_INTERVAL apart.
_DDG_LOCK = threading.Lock()
_DDG_LAST = [0.0]
MIN_INTERVAL = 0.6
MAX_QUERIES = 4
MAX_GUESSES = 3


def _ddg_rate_limit() -> None:
    with _DDG_LOCK:
        wait = MIN_INTERVAL - (time.monotonic() - _DDG_LAST[0])
        if wait > 0:
            time.sleep(wait)
        _DDG_LAST[0] = time.monotonic()


def _unwrap_ddg_redirect(href: str) -> str | None:
    """DDG wraps result links as //duckduckgo.com/l/?uddg=<urlencoded url>."""
    if not href:
        return None
    if href.startswith("//duckduckgo.com/l/") or href.startswith("https://duckduckgo.com/l/"):
        parsed = urllib.parse.urlparse(href if "://" in href else "https:" + href)
        params = urllib.parse.parse_qs(parsed.query)
        target = (params.get("uddg") or [None])[0]
        if target and target.startswith(("http://", "https://")):
            return target
        return None
    if href.startswith(("http://", "https://")):
        return href
    return None


def parse_ddg_html(html: str, *, query: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    blocks = re.split(r'<div[^>]*class="[^"]*result[^"]*"[^>]*>', html)
    for block in blocks[1:]:
        anchor = re.search(r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not anchor:
            continue
        url = _unwrap_ddg_redirect(anchor.group(1).replace("&amp;", "&"))
        if not url:
            continue
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", anchor.group(2))).strip()
        snippet_match = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', block, re.S)
        snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", snippet_match.group(1))).strip() if snippet_match else ""
        results.append({
            "url": url,
            "title": title,
            "snippet": snippet,
            "rank": len(results) + 1,
            "provider": "duckduckgo_html",
            "query": query,
        })
        if len(results) >= 12:
            break
    return results


def _is_challenge(html: str) -> bool:
    low = html.lower()
    return "anomaly" in low or ("challenge" in low and "bots use" in low) or "result__a" not in html and len(html) < 20000 and "duckduckgo" in low and "result" not in low


def ddg_search_query(query: str, *, timeout: float = 8.0) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One DuckDuckGo HTML query (no API key). Returns results in the same
    shape as the Brave web results plus audit hashes."""
    url = DDG_HTML_ENDPOINT + "?" + urllib.parse.urlencode({"q": query})
    request = urllib.request.Request(url, headers={
        "User-Agent": DDG_UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "nb-NO,nb;q=0.9,en;q=0.8",
    })
    started = time.monotonic()
    _ddg_rate_limit()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(2_000_000)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        html = raw.decode("utf-8", "replace")
        metrics = {
            "requests": 1,
            "bytes": len(raw),
            "latencies_ms": [elapsed_ms],
            "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "results_sha256": hashlib.sha256(raw).hexdigest(),
            "challenged": _is_challenge(html),
            "provider": "duckduckgo_html",
        }
        return parse_ddg_html(html, query=query), metrics
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return [], {
            "requests": 1,
            "bytes": 0,
            "latencies_ms": [elapsed_ms],
            "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "challenged": True,
            "provider": "duckduckgo_html",
            "error": f"{type(exc).__name__}: {getattr(exc, 'code', exc)}",
        }


def _name_tokens(name: str) -> list[str]:
    text = str(name or "").translate(str.maketrans({"ø": "o", "å": "a", "æ": "ae", "Ø": "O", "Å": "A", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return [t for t in re.findall(r"[a-z0-9]+", text) if t not in GENERIC_NAME_TOKENS and len(t) > 2]


def domain_guesses(profile: dict[str, Any], limit: int = MAX_GUESSES) -> list[str]:
    """Deterministic .no domain candidates from the distinctive legal name.

    These are only crawl candidates: publication still requires the fetched
    page to pass the exact-entity identity gate (org number or full legal
    name on the homepage)."""
    toks = _name_tokens(profile.get("name"))
    if not toks:
        return []
    cands = [f"https://{toks[0]}.no", f"https://www.{toks[0]}.no"]
    if len(toks) > 1:
        cands.append(f"https://{''.join(toks[:2])}.no")
    seen: set[str] = set()
    out = []
    for c in cands[:limit]:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def discover_official_website(profile: dict[str, Any], *, timeout: float = 8.0) -> tuple[dict[str, Any], dict[str, Any]]:
    """Multi-channel free website discovery.

    Channels: (1) DuckDuckGo HTML search with query variants (abstains when the
    endpoint serves a bot challenge); (2) deterministic .no domain guesses.
    All candidates remain crawl-only until the fetched page passes the
    exact-entity identity gate downstream."""
    base_query = build_company_search_query(profile)
    name = " ".join(str(profile.get("name") or "").split())
    org = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    municipality = " ".join(str(profile.get("municipality") or "").split())
    variants = [
        base_query,
        f'"{name}" {org}',
        f'"{name}" hjemmeside',
        f'"{name}" offisiell nettside',
    ]
    all_results: list[dict[str, Any]] = []
    query_log: list[dict[str, Any]] = []
    challenged = False
    total_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
    selected = None
    for query in variants[:MAX_QUERIES]:
        if challenged:
            break
        results, metrics = ddg_search_query(query, timeout=timeout)
        total_metrics["requests"] += metrics["requests"]
        total_metrics["bytes"] += metrics["bytes"]
        total_metrics["latencies_ms"] += metrics["latencies_ms"]
        query_log.append({
            "query": query,
            "query_sha256": metrics.get("query_sha256"),
            "results_sha256": metrics.get("results_sha256"),
            "n_results": len(results),
            "challenged": metrics.get("challenged"),
            "error": metrics.get("error"),
        })
        if metrics.get("challenged"):
            challenged = True
            continue
        all_results.extend(results)
        choice = choose_search_candidate(profile, all_results)
        if choice.get("selected"):
            selected = choice["selected"]
            break
    if selected is None and all_results:
        selected = choose_search_candidate(profile, all_results).get("selected")
    discovery = {
        "provider": "duckduckgo_html+domain_guesses",
        "queries": query_log,
        "search_challenged": challenged,
        "search_results_count": len(all_results),
        "selected_url": (selected or {}).get("url"),
        "selected_host": (selected or {}).get("host"),
        "selected_score": (selected or {}).get("score"),
        "domain_guesses": domain_guesses(profile),
        "policy": "Search results and domain guesses are only crawl candidates. Publication requires fetched-page exact-entity verification (apply_website_identity_gate).",
        "method": "duckduckgo_html_search_v4 + deterministic_no_domain_guesses_v1",
    }
    return discovery, total_metrics
