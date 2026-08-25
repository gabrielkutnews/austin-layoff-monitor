#!/usr/bin/env python3
"""Austin-area WARN and business-layoff monitor.

Polls official Texas WARN workbooks and Google News RSS, applies transparent
rules, deduplicates related coverage, and sends concise Slack DMs.
"""

import argparse
import copy
import hashlib
import html
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DIR = Path(__file__).resolve().parent
CONFIG_FILE = DIR / "config.json"
STATE_FILE = Path(os.environ.get("LAYOFF_STATE_FILE", str(DIR / "state.json")))
LOCAL_TZ = ZoneInfo("America/Chicago")
TWC_PAGE = "https://www.twc.texas.gov/businesses/worker-adjustment-and-retraining-notification-warn-notices"
TWC_TEMPLATE = "https://www.twc.texas.gov/sites/default/files/oei/docs/warn-act-listings-{}-twc.xlsx"
GOOGLE_NEWS = "https://news.google.com/rss/search"
USER_AGENT = "austin-layoff-monitor/1.0 (personal news monitor)"
HTTP_ERRORS = (urllib.error.URLError, OSError, ValueError, ET.ParseError, zipfile.BadZipFile)

POSITIVE_RE = re.compile(
    r"\b(layoffs?|(?:lay|lays|laid|laying)[ -]off|job cuts?|workforce reductions?|eliminat(?:e|es|ing|ed) "
    r"(?:\d[\d,.]*\s+)?(?:jobs?|positions?|roles?)|cuts? (?:\d[\d,.]*\s+)?"
    r"(?:jobs?|positions?|roles?)|cutting (?:\w+\s+){0,3}(?:jobs?|positions?|roles?)|"
    r"plant clos(?:ure|ing)|facility clos(?:ure|ing)|"
    r"shutters?|restructuring)\b", re.I)
NEGATIVE_RES = [
    re.compile(p, re.I) for p in [
        r"\b(avoids?|averts?|prevents?|no|not|denies?|denied|reverses?|reversed) layoffs?\b",
        r"\b(fears?|rumors?|could|may|might|loom(?:s|ed|ing)?|threat(?:s|ens|ened)?)\b",
        r"\b(after|following) layoffs?\b", r"\bformer (?:employees?|workers?)\b",
        r"\brall(?:y|ies|ied)\b", r"\bprotests?\b", r"\blawsuits?\b",
        r"\b(unemployment|jobless) claims?\b", r"\byears? (?:after|since)\b",
        r"\banniversary\b", r"\bhistory of\b"
    ]
]
CLOSURE_RE = re.compile(r"\b(plant|facility|office|site|store|factory|campus)\b.{0,30}\b(close[sd]?|closing|closure|shutter(?:s|ed|ing)?)\b", re.I)
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "in",
    "is", "it", "of", "on", "or", "the", "to", "with", "will", "after", "amid",
    "company", "companies", "jobs", "job", "cuts", "cut", "layoff", "layoffs"
}


def log(message):
    print(datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def now_iso(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalize_text(value):
    value = html.unescape(value or "").lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9%]+", " ", value)).strip()


def slack_escape(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return copy.deepcopy(default)


def load_config():
    cfg = load_json(CONFIG_FILE, {})
    cfg["county_set"] = {normalize_text(x) for x in cfg["counties"]}
    cfg["city_terms"] = [normalize_text(x) for x in cfg["cities"]]
    cfg["employer_aliases"] = {
        name: sorted({normalize_text(name), *(normalize_text(a) for a in aliases)}, key=len, reverse=True)
        for name, aliases in cfg["priority_employers"].items()
    }
    cfg["publisher_domains"] = {x.lower().lstrip(".") for x in cfg["publisher_domains"]}
    return cfg


def read_secrets():
    secrets = {}
    try:
        secrets = json.loads(os.environ.get("ALL_SECRETS", "") or "{}")
    except ValueError:
        pass
    token = (secrets.get("SLACK_BOT_TOKEN") or os.environ.get("SLACK_BOT_TOKEN") or "").strip()
    if not token:
        try:
            p = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", "austin-fire-monitor",
                 "-a", "slack-bot-token", "-w"], capture_output=True, text=True, timeout=10)
            if p.returncode == 0:
                token = p.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    raw_ids = secrets.get("SLACK_USER_IDS") or os.environ.get("SLACK_USER_IDS") or ""
    user_ids = [x for x in re.split(r"[,\s]+", raw_ids.strip()) if x.startswith("U")]
    return token, user_ids


def request_bytes(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read(), {
                "etag": response.headers.get("ETag", ""),
                "last_modified": response.headers.get("Last-Modified", "")
            }
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, b"", {}
        raise


def post_slack(text, token, user_ids, dry_run):
    if dry_run:
        log("dry-run, message would be:\n" + text)
        return
    for user_id in user_ids:
        payload = json.dumps({"channel": user_id, "text": text, "unfurl_links": False}).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage", data=payload,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                     "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise OSError("Slack error: " + result.get("error", "unknown"))


# -------------------------------------------------------------------- WARN

def discover_warn_urls(now=None):
    """Return the annual TWC resources applicable today.

    TWC uses a stable year-bearing URL. Building it directly avoids putting
    the Drupal landing page (which is occasionally extremely slow) on the
    critical five-minute polling path. A 404 is isolated and retried, so a
    future naming change cannot block news monitoring.
    """
    today = (now or datetime.now(timezone.utc)).date()
    years = [today.year]
    if (today - date(today.year, 1, 1)).days < 90:
        years.append(today.year - 1)
    return {year: TWC_TEMPLATE.format(year) for year in years}


def excel_date(value):
    if value in (None, ""):
        return ""
    try:
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).date().isoformat()
    except (ValueError, TypeError, OverflowError):
        return str(value).strip()


def _xlsx_shared_strings(archive):
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    return ["".join(t.text or "" for t in item.findall(".//m:t", ns))
            for item in root.findall("m:si", ns)]


def parse_warn_xlsx(data):
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        strings = _xlsx_shared_strings(archive)
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    matrix = []
    for row in sheet.findall(".//m:sheetData/m:row", ns):
        values = {}
        for cell in row.findall("m:c", ns):
            ref = cell.get("r", "A1")
            letters = re.match(r"[A-Z]+", ref).group(0)
            col = 0
            for letter in letters:
                col = col * 26 + ord(letter) - 64
            inline = cell.find("m:is", ns)
            value_node = cell.find("m:v", ns)
            value = ""
            if inline is not None:
                value = "".join(t.text or "" for t in inline.findall(".//m:t", ns))
            elif value_node is not None:
                value = value_node.text or ""
                if cell.get("t") == "s" and value:
                    value = strings[int(value)]
            values[col - 1] = value
        if values:
            matrix.append([values.get(i, "") for i in range(max(values) + 1)])
    if not matrix:
        return []
    headers = [normalize_text(x).replace(" ", "_") for x in matrix[0]]
    rows = []
    for values in matrix[1:]:
        row = dict(zip(headers, values))
        if not row.get("job_site_name"):
            continue
        for field in ("notice_date", "layoff_date", "w_f_d_d_received_date", "wfdd_received_date"):
            if field in row:
                row[field] = excel_date(row[field])
        try:
            row["total_layoff_number"] = int(float(str(row.get("total_layoff_number", 0)).replace(",", "")))
        except ValueError:
            row["total_layoff_number"] = 0
        rows.append(row)
    return rows


def warn_keys(row):
    employer = normalize_text(row.get("job_site_name"))
    city = normalize_text(row.get("city_name"))
    notice = row.get("notice_date", "")
    base = "|".join([employer, city, notice])
    version = "|".join([base, str(row.get("total_layoff_number", 0)), row.get("layoff_date", "")])
    return hashlib.sha256(base.encode()).hexdigest()[:20], hashlib.sha256(version.encode()).hexdigest()[:20]


def format_warn(row, updated=False):
    label = "🔴 *OFFICIAL WARN UPDATE*" if updated else "🔴 *OFFICIAL WARN*"
    employer = slack_escape(row.get("job_site_name", "Unknown employer"))
    city = slack_escape(row.get("city_name") or row.get("county_name") or "Austin area")
    count = int(row.get("total_layoff_number", 0))
    when = " — effective {}".format(row["layoff_date"]) if row.get("layoff_date") else ""
    return "{}\n*{}* — {:,} job{} — {}{}\n<{}|Texas WARN source>".format(
        label, employer, count, "" if count == 1 else "s", city, when, TWC_PAGE)


def process_warn(state, cfg, now=None):
    alerts, changed = [], False
    initialized = state.get("warn_initialized", False)
    events = state.setdefault("warn_events", {})
    files = state.setdefault("warn_files", {})
    successes = 0
    for year, url in discover_warn_urls(now).items():
        old_meta = files.get(str(year), {})
        headers = {}
        if old_meta.get("etag"):
            headers["If-None-Match"] = old_meta["etag"]
        if old_meta.get("last_modified"):
            headers["If-Modified-Since"] = old_meta["last_modified"]
        try:
            status, body, meta = request_bytes(url, headers)
            successes += 1
            if status == 304:
                continue
            digest = hashlib.sha256(body).hexdigest()
            if old_meta.get("sha256") == digest:
                files[str(year)] = {**old_meta, **meta, "url": url, "sha256": digest}
                continue
            rows = parse_warn_xlsx(body)
            for row in rows:
                if normalize_text(row.get("county_name")) not in cfg["county_set"]:
                    continue
                base, version = warn_keys(row)
                previous = events.get(base)
                if previous != version and initialized:
                    alerts.append(format_warn(row, updated=previous is not None))
                events[base] = version
            files[str(year)] = {**meta, "url": url, "sha256": digest, "checked_at": now_iso(now)}
            changed = True
        except HTTP_ERRORS as exc:
            log("WARN {} fetch failed; will retry: {}".format(year, exc))
    if successes and not initialized:
        state["warn_initialized"] = True
        changed = True
        log("WARN: seeded current local notices without backfill")
    return alerts, changed


# -------------------------------------------------------------------- news

def quote_or(terms):
    return "(" + " OR ".join('"{}"'.format(x) if " " in x else x for x in terms) + ")"


def build_news_feeds(cfg):
    positive = quote_or(cfg["layoff_terms"])
    window = "when:{}d".format(cfg["news_window_days"])
    feeds = [("local", "{} {} {}".format(positive, quote_or(cfg["cities"] + cfg["counties"]), window))]
    for index, names in enumerate(cfg["priority_batches"], 1):
        aliases = []
        for name in names:
            aliases.extend(cfg["priority_employers"][name])
        feeds.append(("priority-{}".format(index), "{} {} {}".format(positive, quote_or(aliases), window)))
    feeds.append(("ut-austin", '{} "University of Texas at Austin" {}'.format(positive, window)))
    return [(name, GOOGLE_NEWS + "?" + urllib.parse.urlencode(
        {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})) for name, query in feeds]


def parse_news_rss(data, feed_name=""):
    root = ET.fromstring(data)
    items = []
    for item in root.findall("./channel/item"):
        source = item.find("source")
        title = html.unescape(item.findtext("title") or "").strip()
        source_name = html.unescape(source.text or "").strip() if source is not None else ""
        suffix = " - " + source_name
        if source_name and title.lower().endswith(suffix.lower()):
            title = title[:-len(suffix)].strip()
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(timezone.utc)
        except (TypeError, ValueError):
            published = datetime.now(timezone.utc)
        items.append({
            "guid": item.findtext("guid") or item.findtext("link") or title,
            "title": title,
            "link": item.findtext("link") or "",
            "published": now_iso(published),
            "source": source_name,
            "source_url": source.get("url", "") if source is not None else "",
            "feed": feed_name,
        })
    return items


def publisher_allowed(url, cfg):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return any(host == domain or host.endswith("." + domain) for domain in cfg["publisher_domains"])


def match_company(title, cfg):
    text = " " + normalize_text(title) + " "
    for company, aliases in cfg["employer_aliases"].items():
        if any(re.search(r"\b" + re.escape(alias) + r"\b", text) for alias in aliases):
            return company
    return ""


def is_local(title, cfg):
    text = " " + normalize_text(title) + " "
    return any(re.search(r"\b" + re.escape(term) + r"\b", text) for term in cfg["city_terms"] + list(cfg["county_set"]))


def extract_numbers(title):
    # Preserve commas and decimal points here; normalize_text intentionally
    # removes punctuation and would turn "1,000" into two unrelated numbers.
    text = re.sub(r"\s+", " ", html.unescape(title or "").lower())
    counts = []
    number = r"(\d[\d,]*(?:\.\d+)?\s*k?)"
    units = r"(?:jobs?|workers?|employees?|positions?|roles?|staff|people|layoffs?)"
    qualifier = r"(?:(?:about|around|nearly|over|more than|at least|another)\s+)?"
    patterns = [number + r"\s+" + units,
                r"(?:cut|cuts|cutting|lay off|lays off|laid off|laying off|eliminate\w*)\s+" + qualifier + number]
    for pattern in patterns:
        for raw in re.findall(pattern, text, re.I):
            compact = raw.replace(" ", "").replace(",", "").lower()
            try:
                value = float(compact[:-1]) * 1000 if compact.endswith("k") else float(compact)
                if value >= 5:
                    counts.append(int(value))
            except ValueError:
                pass
    if re.search(r"\bthousands\b", text):
        counts.append(1000)
    elif re.search(r"\bhundreds\b", text):
        counts.append(200)
    percentages = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", text)]
    return max(counts) if counts else None, max(percentages) if percentages else None


def title_tokens(title):
    return {x for x in normalize_text(title).split() if len(x) > 2 and x not in STOP_WORDS and not x.isdigit()}


def title_similarity(left, right):
    a, b = set(left), set(right)
    return len(a & b) / len(a | b) if a and b else 0.0


def public_employer(title, company, cfg):
    if company == "UT Austin":
        return False
    low = " " + normalize_text(title) + " "
    return any(" " + normalize_text(pattern) + " " in low
               for pattern in cfg["public_employer_patterns"])


def classify_article(item, cfg):
    title = item["title"]
    if not publisher_allowed(item["source_url"], cfg):
        return None, "publisher not allowlisted"
    if not POSITIVE_RE.search(title):
        return None, "no affirmative layoff phrase"
    if any(pattern.search(title) for pattern in NEGATIVE_RES):
        return None, "negative, speculative, historical, or follow-up language"
    company = match_company(title, cfg)
    local = is_local(title, cfg)
    count, percent = extract_numbers(title)
    closure = bool(CLOSURE_RE.search(title))
    if public_employer(title, company, cfg):
        return None, "excluded public employer"
    if company and local:
        scope, label = "local", "🟠 *REPORTED LOCAL LAYOFF*"
    elif company and ((count or 0) >= cfg["priority_companywide_jobs"] or
                      (percent or 0) >= cfg["priority_companywide_percent"]):
        scope, label = "companywide", "🟡 *MAJOR EMPLOYER — AUSTIN IMPACT UNKNOWN*"
    elif local and ((count or 0) >= cfg["local_job_threshold"] or closure):
        scope, label = "local", "🟠 *REPORTED LOCAL LAYOFF*"
    elif item["feed"] == "local" and not local:
        return None, "Austin area not present in headline"
    elif company:
        return None, "priority-company story below companywide threshold and not explicitly local"
    else:
        return None, "local story below 50 jobs without an explicit facility closure"
    display_company = company or "Austin-area employer"
    return {
        "company": display_company, "scope": scope, "count": count, "percent": percent,
        "closure": closure, "tokens": sorted(title_tokens(title)), "title": title,
        "published": item["published"], "source": item["source"], "link": item["link"],
        "guid": item["guid"], "label": label,
    }, "accepted"


def find_cluster(event, clusters):
    published = parse_iso(event["published"])
    for key, old in clusters.items():
        try:
            age = abs((published - parse_iso(old["published"])).total_seconds())
        except (ValueError, KeyError):
            continue
        if age > 72 * 3600 or old.get("company") != event["company"]:
            continue
        same_count = (event["company"] != "Austin-area employer" and
                      event.get("count") is not None and event.get("count") == old.get("count"))
        if same_count or title_similarity(event["tokens"], old.get("tokens", [])) >= 0.55:
            return key, old
    return None, None


def format_news(event, update=False):
    update_text = " — *UPDATE*" if update else ""
    metrics = []
    if event.get("count"):
        metrics.append("{:,.0f} jobs reported".format(event["count"]))
    if event.get("percent"):
        metrics.append("{:g}% of workforce".format(event["percent"]))
    if event.get("closure"):
        metrics.append("facility closure")
    detail = " — " + ", ".join(metrics) if metrics else ""
    return "{}{}\n*{}*{}\n{} — <{}|{}>".format(
        event["label"], update_text, slack_escape(event["company"]), detail,
        slack_escape(event["title"]), event["link"], slack_escape(event["source"] or "source"))


def process_news(state, cfg, show_suppressed=False, now=None):
    initialized = state.get("news_initialized", False)
    seen = state.setdefault("news_seen", {})
    clusters = state.setdefault("news_clusters", {})
    alerts, changed, successes = [], False, 0
    fetched = []

    def fetch_feed(name, url):
        _, body, _ = request_bytes(url)
        return name, parse_news_rss(body, name)

    feeds = build_news_feeds(cfg)
    with ThreadPoolExecutor(max_workers=len(feeds)) as pool:
        futures = {pool.submit(fetch_feed, name, url): name for name, url in feeds}
        for future in as_completed(futures):
            name = futures[future]
            try:
                _, items = future.result()
                successes += 1
                fetched.extend(items)
            except HTTP_ERRORS as exc:
                log("news {} failed; will retry: {}".format(name, exc))
    current = now or datetime.now(timezone.utc)
    newest_allowed = current + timedelta(hours=6)
    oldest_allowed = current - timedelta(days=cfg["news_window_days"], hours=6)
    for item in fetched:
        try:
            published = parse_iso(item["published"])
        except ValueError:
            continue
        if not oldest_allowed <= published <= newest_allowed:
            continue
        guid = hashlib.sha256(item["guid"].encode()).hexdigest()[:24]
        if guid in seen:
            continue
        seen[guid] = now_iso(now)
        changed = True
        event, reason = classify_article(item, cfg)
        if not event:
            if show_suppressed:
                log('news suppressed [{}]: "{}"'.format(reason, item["title"]))
            continue
        key, old = find_cluster(event, clusters)
        if old:
            material_update = (old.get("scope") == "companywide" and event["scope"] == "local") or (
                event.get("count") is not None and event.get("count") != old.get("count"))
            if material_update and initialized:
                alerts.append(format_news(event, update=True))
            clusters[key] = event
        else:
            key = hashlib.sha256((event["company"] + "|" + event["published"] + "|" + event["title"]).encode()).hexdigest()[:24]
            clusters[key] = event
            if initialized:
                alerts.append(format_news(event))
    if successes and not initialized:
        state["news_initialized"] = True
        changed = True
        log("news: seeded current RSS results without backfill")
    return alerts, changed


def prune_state(state, cfg, now=None):
    current = now or datetime.now(timezone.utc)
    guid_cutoff = current - timedelta(days=cfg["news_guid_retention_days"])
    event_cutoff = current - timedelta(days=cfg["event_retention_days"])
    state["news_seen"] = {k: v for k, v in state.get("news_seen", {}).items()
                          if parse_iso(v) >= guid_cutoff}
    state["news_clusters"] = {k: v for k, v in state.get("news_clusters", {}).items()
                              if parse_iso(v["published"]) >= event_cutoff}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print messages instead of sending Slack DMs")
    parser.add_argument("--show-suppressed", action="store_true", help="log why each rejected news result was suppressed")
    args = parser.parse_args(argv)
    cfg = load_config()
    token, user_ids = read_secrets()
    if not args.dry_run and (not token.startswith("xoxb-") or not user_ids):
        log("ERROR: configure SLACK_BOT_TOKEN and SLACK_USER_IDS")
        return 2
    original = load_json(STATE_FILE, {})
    state = copy.deepcopy(original)
    warn_alerts, warn_changed = process_warn(state, cfg)
    news_alerts, news_changed = process_news(state, cfg, args.show_suppressed)
    prune_state(state, cfg)
    alerts = warn_alerts + news_alerts
    if alerts:
        try:
            post_slack("\n\n".join(alerts[:20]), token, user_ids, args.dry_run)
        except (urllib.error.URLError, OSError) as exc:
            log("Slack delivery failed; state not saved: {}".format(exc))
            return 1
    if warn_changed or news_changed or state != original:
        STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    log("complete: {} WARN + {} news alert(s)".format(len(warn_alerts), len(news_alerts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
