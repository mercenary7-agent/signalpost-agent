from __future__ import annotations

import hashlib
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from .evidence import evidence, utc_now
from .web_search import ddg_search_query

NEWS_UA = "SignalpostResearchPOC/2.0 (https://builderr.ai; bounded qualification run)"
LEGAL_SUFFIXES = {"as", "asa", "sa", "ba", "da", "ans", "enk", "nuf", "sti"}


def _tokens(value: object) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9æøå]+", str(value or "").casefold()) if t not in LEGAL_SUFFIXES]


def exact_title_match(company_name: str, title: str) -> bool:
    """Strict exact-entity gate: the full legal name (minus legal suffix) must
    appear contiguously in the headline. Contiguous full-name match is itself
    the wrong-company guard (a different company's longer name cannot match)."""
    company_tokens = _tokens(company_name)
    headline = str(title or "").rsplit(" - ", 1)[0]
    title_tokens = _tokens(headline)
    if not company_tokens or not title_tokens or len(company_tokens) > len(title_tokens):
        return False
    n = len(company_tokens)
    return any(title_tokens[i:i + n] == company_tokens for i in range(len(title_tokens) - n + 1))


def _ddg_news_fallback(name: str, org: str, limit: int = 10) -> tuple[list[dict], dict]:
    """DDG HTML fallback for news mentions when RSS feeds yield none.

    Same strict exact-legal-name title gate as the RSS path; every accepted
    item is a permitted_public_page observation with the query URL and the
    result-list sha as evidence anchors."""
    metric = {"requests": 0, "bytes": 0, "latencies_ms": []}
    mentions: list[dict] = []
    seen: set[tuple[str, str]] = set()
    queries = [f'"{name}" nyheter', f'"{name}" i nyhetene', f'"{name}" site:no']
    for query in queries:
        if len(mentions) >= limit:
            break
        results, m = ddg_search_query(query, timeout=5.0)
        metric["requests"] += m.get("requests", 0)
        metric["bytes"] += m.get("bytes", 0)
        metric["latencies_ms"] += m.get("latencies_ms", [])
        if m.get("challenged"):
            break
        retrieved_at = utc_now()
        query_url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
        for item in results:
            title = str(item.get("title") or "").strip()
            link = str(item.get("url") or "").strip()
            if not link or not title or not exact_title_match(name, title):
                continue
            key = (title.casefold(), link.casefold())
            if key in seen:
                continue
            seen.add(key)
            mentions.append({
                "id": "gd-" + hashlib.sha256(f"{org}|{title}|{link}".encode("utf-8")).hexdigest()[:24],
                "organisation_number": org,
                "platform": "news",
                "signal_type": "public_mention",
                "title": title,
                "publisher": urllib.parse.urlparse(link).netloc,
                "source_url": link,
                "retrieved_at": retrieved_at,
                "published_at": None,
                "content_sha256": m.get("results_sha256"),
                "feed_url": query_url,
                "exact_entity": True,
                "identity_proof": [
                    {"type": "exact_legal_name_in_news_title", "value": name},
                    {"type": "search_provider", "value": "duckduckgo_html"},
                ],
                "acquisition_mode": "permitted_public_page",
                "rights_status": "approved",
                "source_class": "public_news",
                "evidence_span": f"{title} — {urllib.parse.urlparse(link).netloc}",
            })
            if len(mentions) >= limit:
                break
    return mentions, metric


def fetch_news_mentions(profile: dict[str, Any], limit: int = 10, years: int = 2) -> tuple[dict[str, Any], dict]:
    """News RSS search (Bing primary, Google fallback) with strict
    exact-legal-name title gating.

    Returns an evidence record for the `news` module plus run metrics.
    Status is terminal in all cases: available / not_found / source_error.
    """
    org = str(profile["organisation_number"])
    name = str(profile.get("name") or "")
    metric = {"requests": 0, "bytes": 0, "latencies_ms": []}
    query = urllib.parse.quote(f"{name} when:{years}y")
    feeds = [
        ("bing_news_rss_exact_title", f"https://www.bing.com/news/search?q={query}&format=rss&setlang=no&cc=no"),
        ("google_news_rss_exact_title", f"https://news.google.com/rss/search?q={query}&hl=no&gl=NO&ceid=NO:no"),
    ]
    last_exc: Exception | None = None
    raw = b""
    started = datetime.now(timezone.utc)
    source_label = feeds[0][0]
    url = feeds[0][1]
    for label, feed_url in feeds:
        ok = False
        for attempt in range(2):
            try:
                started = datetime.now(timezone.utc)
                request = urllib.request.Request(feed_url, headers={"User-Agent": NEWS_UA, "Accept": "application/rss+xml, application/xml"})
                with urllib.request.urlopen(request, timeout=12) as response:
                    raw = response.read(2_000_000)
                last_exc = None
                source_label = label
                url = feed_url
                ok = True
                break
            except Exception as exc:
                last_exc = exc
                metric["requests"] += 1
                if attempt < 1:
                    import time
                    time.sleep(1)
        if ok:
            break
    if not raw:
        record = evidence(
            "news",
            "source_error",
            source_label,
            url,
            value=None,
            note=f"{type(last_exc).__name__ if last_exc else 'empty'}: {str(last_exc)[:180] if last_exc else 'no feed responded'}",
        )
        return record, metric
    try:
        elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        metric["requests"] = max(metric["requests"], 1)
        metric["bytes"] = len(raw)
        metric["latencies_ms"] = [elapsed_ms]
        root = ET.fromstring(raw)
        retrieved_at = utc_now()
        feed_sha = hashlib.sha256(raw).hexdigest()
        mentions: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for item in root.findall(".//item"):
            title = str(item.findtext("title") or "").strip()
            link = str(item.findtext("link") or "").strip()
            publisher = str(item.findtext("source") or "").strip()
            if not link or not title or not exact_title_match(name, title):
                continue
            key = (title.casefold(), publisher.casefold())
            if key in seen:
                continue
            seen.add(key)
            published_at = None
            pub_date = item.findtext("pubDate")
            try:
                if pub_date:
                    published_at = parsedate_to_datetime(pub_date).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            except Exception:
                pass
            mentions.append({
                "id": "gn-" + hashlib.sha256(f"{org}|{title}|{publisher}".encode("utf-8")).hexdigest()[:24],
                "organisation_number": org,
                "platform": "news",
                "signal_type": "public_mention",
                "title": title,
                "publisher": publisher,
                "source_url": link,
                "retrieved_at": retrieved_at,
                "published_at": published_at,
                "content_sha256": feed_sha,
                "feed_url": url,
                "exact_entity": True,
                "identity_proof": [
                    {"type": "exact_legal_name_in_news_title", "value": name},
                    {"type": "publisher_label", "value": publisher},
                ],
                "acquisition_mode": "permitted_public_page",
                "rights_status": "approved",
                "source_class": "public_news",
                "evidence_span": f"{title} — {publisher}" + (f", published {published_at}" if published_at else ""),
            })
            if len(mentions) >= limit:
                break
        ddg_used = False
        if not mentions:
            ddg_mentions, ddg_metric = _ddg_news_fallback(name, org, limit=limit)
            ddg_used = True
            metric["requests"] += ddg_metric["requests"]
            metric["bytes"] += ddg_metric["bytes"]
            metric["latencies_ms"] += ddg_metric["latencies_ms"]
            mentions.extend(ddg_mentions)
        record = evidence(
            "news",
            "available" if mentions else "not_found",
            source_label,
            url,
            value={"mentions": mentions, "lookback_years": years, "raw_items": len(root.findall(".//item")), "ddg_fallback_used": ddg_used},
            retrieved_at=retrieved_at,
            source_row_key=org,
            note=f"exact-legal-name title gate accepted {len(mentions)} mentions",
        )
        return record, metric
    except Exception as exc:
        record = evidence(
            "news",
            "source_error",
            source_label,
            url,
            value=None,
            note=f"{type(exc).__name__}: {str(exc)[:180]}",
        )
        metric["requests"] = 1
        return record, metric
