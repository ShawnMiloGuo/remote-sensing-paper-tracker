#!/usr/bin/env python3
"""Issue-aware harvesting using publisher RSS and Crossref metadata.

Crossref volume matching is not a proof of complete publisher issue coverage.
This script reports coverage honestly and preserves user-maintained state.
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import feedparser
import requests

ROOT = Path(__file__).resolve().parents[1]
JOURNALS = {
    "JAG": {"issn": "1569-8432", "rss": "https://rss.sciencedirect.com/publication/science/15698432"},
    "RSE": {"issn": "0034-4257", "rss": "https://rss.sciencedirect.com/publication/science/00344257"},
    "ISPRS JPRS": {"issn": "0924-2716", "rss": "https://rss.sciencedirect.com/publication/science/09242716"},
    "TGRS": {"issn": "0196-2892", "rss": None},
}
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "RemoteSensingPaperTracker/1.0 (metadata research; contact via GitHub repository)", "Accept": "application/json, application/xml, text/xml, */*"})
MAILTO = os.getenv("CROSSREF_MAILTO", "").strip()
if MAILTO:
    SESSION.headers["User-Agent"] += f" mailto:{MAILTO}"


def fetch(url, params=None):
    last = None
    for attempt in range(3):
        try:
            r = SESSION.get(url, params=params, timeout=35)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"{url}: {last or 'rate-limited'}")


def crossref_items(issn, filters=None, rows=1000, max_pages=20):
    params = {"rows": min(rows, 1000), "select": "DOI,title,author,published,volume,issue,type,URL,container-title"}
    if filters:
        params["filter"] = filters
    items = []
    for offset in range(max_pages):
        params["offset"] = offset * params["rows"]
        msg = fetch(f"https://api.crossref.org/journals/{quote(issn)}/works", params=params).json()["message"]
        batch = msg.get("items", [])
        items.extend(batch)
        if len(batch) < params["rows"]:
            break
    else:
        raise RuntimeError("Crossref pagination limit reached; completeness unknown")
    return items


def norm_doi(value):
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(value or ""), flags=re.I).strip().lower()


def date_of(item):
    parts = (item.get("published") or {}).get("date-parts", [[]])[0]
    return "-".join(str(x).zfill(2) if i else str(x) for i, x in enumerate(parts))


def title_of(item):
    return (item.get("title") or [""])[0].strip()


def authors_of(item):
    return "; ".join(" ".join(filter(None, [a.get("given"), a.get("family")])) for a in item.get("author", []))


def read_state():
    p = ROOT / "state.json"
    state = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    state.setdefault("papers", {})
    return state


def rss_dois(url):
    raw = fetch(url).content
    feed = feedparser.parse(raw)
    if feed.bozo and not feed.entries:
        raise RuntimeError(str(feed.bozo_exception))
    found = set()
    for e in feed.entries:
        candidates = [e.get("id", ""), e.get("link", ""), e.get("dc_identifier", ""), e.get("doi", "")]
        for value in candidates:
            match = re.search(r"10\.\d{4,9}/[^\s<>\"&]+", str(value), re.I)
            if match:
                found.add(norm_doi(match.group(0).rstrip(".,;)")))
    return found, len(feed.entries)


def main():
    state = read_state()
    old = {norm_doi(k) for k in state["papers"]}
    report = {"run_at": datetime.now(timezone.utc).isoformat(), "journals": {}, "notes": "Crossref metadata cannot certify complete publisher issue contents; check official table of contents."}
    additions = []
    for journal, cfg in JOURNALS.items():
        info = {"rss_entries": None, "rss_dois": None, "crossref_records": None, "target_volume": None, "target_issue": None, "issue_completeness_verified": False, "new_records": 0, "errors": []}
        report["journals"][journal] = info
        rss = set()
        if cfg["rss"]:
            try:
                rss, count = rss_dois(cfg["rss"])
                info["rss_entries"], info["rss_dois"] = count, len(rss)
            except Exception as e:
                info["errors"].append("RSS: " + str(e))
        try:
            # Fetch recent indexed works, then infer latest assigned volume; RSS is supplementary.
            items = crossref_items(cfg["issn"], rows=1000, max_pages=3)
            assigned = [x for x in items if x.get("volume") and x.get("DOI")]
            if not assigned:
                raise RuntimeError("No Crossref records with assigned volume")
            def volume_key(x):
                v = x.get("volume", "")
                return (int(v) if v.isdigit() else -1, date_of(x))
            latest = max(assigned, key=volume_key)
            volume = latest["volume"]
            info["target_volume"] = volume
            # Crossref indexes volume; some journals have issue numbers, others do not.
            vol_items = crossref_items(cfg["issn"], filters=f"from-pub-date:2025-01-01", rows=1000, max_pages=20)
            selected = [x for x in vol_items if str(x.get("volume", "")) == str(volume) and x.get("DOI") and x.get("type") == "journal-article"]
            info["crossref_records"] = len(selected)
            issue_values = sorted({str(x.get("issue")) for x in selected if x.get("issue")})
            info["target_issue"] = issue_values[-1] if len(issue_values) == 1 else None
            for x in selected:
                doi = norm_doi(x["DOI"])
                if doi in old:
                    continue
                title = title_of(x)
                if not title:
                    continue
                record = {"journal": journal, "doi": doi, "title_en": title, "title_zh": "", "authors": authors_of(x), "affiliations_zh": "作者单位暂未获取", "publication_date": date_of(x), "volume": str(volume), "issue": str(x.get("issue") or ""), "summary_zh": "", "url": "https://doi.org/" + doi, "first_pushed_at": datetime.now(timezone.utc).date().isoformat(), "favorite": False, "read": False, "metadata_source": "Crossref", "verification": "volume metadata matched; official issue completeness not verified"}
                additions.append(record)
                state["papers"][doi] = record
                old.add(doi)
                info["new_records"] += 1
            info["rss_dois_not_in_volume"] = len(rss - {norm_doi(x["DOI"]) for x in selected})
        except Exception as e:
            info["errors"].append("Crossref: " + str(e))
    if additions:
        with (ROOT / "papers.md").open("a", encoding="utf-8") as f:
            for x in additions:
                f.write(f"\n### {x['journal']} · Volume {x['volume']}｜{x['title_en']}\n")
                f.write(f"- 英文标题：{x['title_en']}\n- DOI：[{x['doi']}]({x['url']})\n")
                f.write(f"- 作者：{x['authors']}\n- 卷期：Volume {x['volume']}" + (f", Issue {x['issue']}" if x['issue'] else "") + "\n")
                f.write(f"- 发表日期：{x['publication_date']}\n- 中文标题及摘要：待翻译\n- 状态：未读、未收藏\n")
    state["last_checked_at"] = report["run_at"]
    (ROOT / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ROOT / "harvest_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
