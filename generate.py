#!/usr/bin/env python3
"""
Samsung TV Plus (US / CA / UK) — M3U + extended EPG.

Two sources, merged per channel:
  1. Matt Huisman's mirror (i.mjh.nz/SamsungTVPlus) — every channel, but only a
     rolling ~6 hours of guide.
  2. tvtv.us Gracenote listings — whole days ahead, for the Samsung channels that
     have a known Gracenote station (samsung_gracenote_map.csv, ~278 US channels).
     Endpoint + Samsung->Gracenote mapping courtesy of kineticman/FastChannels.

For a mapped channel, tvtv's airings replace Matt's from the point tvtv starts;
Matt's description/episode info is copied onto matching airings where the two
overlap. Safety check: if tvtv's "now playing" doesn't resemble Matt's, the
mapping is assumed wrong and that channel stays on Matt's data (see report).
If tvtv is unreachable, the build still writes the Matt-only guide.

Outputs: samsung.m3u, samsung.xml.gz, samsung_report.txt
"""
import csv, gzip, json, os, re, sys, time, unicodedata, difflib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr
import xml.etree.ElementTree as ET

import requests

# ── config ────────────────────────────────────────────────────────────────────
REGIONS     = ["us", "ca", "gb"]                 # Matt region codes (gb = UK)
REGION_LABEL = {"us": "US", "ca": "CA", "gb": "UK"}
TVTV_DAYS   = int(os.getenv("TVTV_DAYS", "3"))   # UTC days fetched: today + N-1
TVTV_WORKERS = 6
KEEP_PAST_H = 3                                  # keep this many hours of past guide
SKIP_DRM    = True                               # DRM channels won't play in TiviMate

MATT_CHANNELS = "https://i.mjh.nz/SamsungTVPlus/.channels.json.gz"
MATT_EPG      = "https://i.mjh.nz/SamsungTVPlus/{region}.xml.gz"
STREAM_URL    = "https://jmp2.uk/stvp-{id}"
TVTV_BASE     = "https://tvtv.us"
MAP_FILE      = "samsung_gracenote_map.csv"
OUT_M3U, OUT_EPG, OUT_REPORT = "samsung.m3u", "samsung.xml.gz", "samsung_report.txt"

REPO = os.getenv("GITHUB_REPOSITORY", "s-digweed/Sammy")
EPG_RAW = f"https://raw.githubusercontent.com/{REPO}/output/{OUT_EPG}"   # outputs live on the "output" branch

# offline testing hooks (not used in Actions)
MATT_CHANNELS_FILE = os.getenv("MATT_CHANNELS_FILE")
MATT_EPG_FILE      = os.getenv("MATT_EPG_FILE")
TVTV_FIXTURE_DIR   = os.getenv("TVTV_FIXTURE_DIR")

NOW = datetime.now(timezone.utc)
report = []
def log(msg):
    print(msg, flush=True); report.append(msg)

# ── helpers ───────────────────────────────────────────────────────────────────
def xmltv_ts(dt):  return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")
def parse_xmltv(s):
    d, off = s.split()[0], (s.split() + ["+0000"])[1]
    base = datetime.strptime(d, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    sign = 1 if off[0] == "+" else -1
    return base - sign * timedelta(hours=int(off[1:3]), minutes=int(off[3:5]))

def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("&", "and")
    s = re.sub(r"\b(the|a|an)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()

def similar(a, b):
    a, b = norm(a), norm(b)
    if not a or not b: return False
    if a == b or a in b or b in a: return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.6

def get_gz(url, path=None):
    if path:
        with open(path, "rb") as f: raw = f.read()
    else:
        r = requests.get(url, timeout=60, headers={"User-Agent": "okhttp/4.12.0"})
        r.raise_for_status(); raw = r.content
    return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw

# ── 1. Matt: channels + ~6h EPG ───────────────────────────────────────────────
def load_matt():
    data = json.loads(get_gz(MATT_CHANNELS, MATT_CHANNELS_FILE))
    chans, drm = {}, 0
    for rg in REGIONS:
        for cid, c in data["regions"].get(rg, {}).get("channels", {}).items():
            if SKIP_DRM and c.get("license_url"):
                drm += 1; continue
            chans[cid] = {"name": c["name"].strip(), "logo": c.get("logo", ""),
                          "group": c.get("group", "Other"), "chno": c.get("chno"), "region": rg}
    progs = {cid: [] for cid in chans}
    for rg in REGIONS:
        try:
            root = ET.fromstring(get_gz(MATT_EPG.format(region=rg), MATT_EPG_FILE))
        except Exception as e:
            log(f"!! Matt EPG {rg} failed: {e}"); continue
        for p in root.findall("programme"):
            cid = p.get("channel")
            if cid not in progs: continue
            ep = {"start": parse_xmltv(p.get("start")), "stop": parse_xmltv(p.get("stop")),
                  "title": p.findtext("title") or "", "sub": p.findtext("sub-title"),
                  "desc": p.findtext("desc"), "icon": None, "epnum": []}
            ic = p.find("icon")
            if ic is not None: ep["icon"] = ic.get("src")
            for en in p.findall("episode-num"):
                ep["epnum"].append((en.get("system"), en.text))
            progs[cid].append(ep)
    for cid, v in progs.items():                 # sort + de-duplicate by start time
        v.sort(key=lambda x: x["start"])
        progs[cid] = [p for i, p in enumerate(v) if i == 0 or p["start"] != v[i - 1]["start"]]
    log(f"Matt: {len(chans)} channels ({', '.join(REGION_LABEL[r] for r in REGIONS)}), "
        f"{sum(map(len, progs.values()))} programmes, {drm} DRM channels skipped")
    return chans, progs

# ── 2. tvtv.us Gracenote listings ─────────────────────────────────────────────
def tvtv_session():
    try:
        from curl_cffi import requests as creq
        s = creq.Session(impersonate="chrome")
        kind = "curl_cffi/chrome"
    except ImportError:
        s = requests.Session(); kind = "plain requests (curl_cffi missing!)"
        s.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
    s.headers.update({"Accept": "text/html, */*", "HX-Request": "true", "Referer": f"{TVTV_BASE}/"})
    try:
        r = s.get(f"{TVTV_BASE}/", timeout=20)
        log(f"tvtv: session via {kind}, homepage HTTP {r.status_code}")
    except Exception as e:
        log(f"tvtv: session via {kind}, homepage prime failed ({e})")
    return s

def parse_fragment(html):
    from bs4 import BeautifulSoup, NavigableString
    out = []
    for a in BeautifulSoup(html or "", "html.parser").select(".gridAiring"):
        try:
            start = datetime.fromtimestamp(int(a.get("data-time")) / 1000, tz=timezone.utc)
            mins = int(a.get("data-runtime"))
        except (TypeError, ValueError):
            continue
        sub_el = a.select_one(".gridSubtitle")
        sub = sub_el.get_text(" ", strip=True) if sub_el else None
        t_el = a.find("div", recursive=False)
        if t_el is not None:
            title = t_el.get_text(" ", strip=True)
        else:
            parts = []
            for ch in a.children:
                if isinstance(ch, NavigableString):
                    if str(ch).strip(): parts.append(str(ch).strip())
                elif getattr(ch, "name", None) != "span":
                    t = ch.get_text(" ", strip=True)
                    if t: parts.append(t)
            title = " ".join(parts).strip()
        out.append({"start": start, "stop": start + timedelta(minutes=max(mins, 1)),
                    "title": title or "Unknown", "sub": sub or None})
    return out

def tvtv_station(session, tmsid, day_keys):
    items, seen = [], set()
    for dk in day_keys:
        if TVTV_FIXTURE_DIR:
            fp = os.path.join(TVTV_FIXTURE_DIR, f"{dk}_{tmsid}.html")
            html = open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""
        else:
            r = session.get(f"{TVTV_BASE}/partial/source/{dk}/{tmsid}", timeout=25)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            html = r.text
        for it in parse_fragment(html):
            k = (it["start"], it["title"])
            if k not in seen:
                seen.add(k); items.append(it)
    items.sort(key=lambda x: x["start"])
    for a, b in zip(items, items[1:]):          # trim overlaps
        if b["start"] < a["stop"]: a["stop"] = b["start"]
    return [i for i in items if i["stop"] > i["start"]]

def load_tvtv(chans):
    mapping = {}
    with open(MAP_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["samsung_id"] in chans:
                mapping[row["samsung_id"]] = row["tmsid"]
    day0 = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    day_keys = [int((day0 + timedelta(days=i)).timestamp() * 1000) for i in range(TVTV_DAYS)]
    log(f"tvtv: {len(mapping)} mapped channels in this lineup, {TVTV_DAYS} day(s) each "
        f"({len(mapping) * TVTV_DAYS} requests)")
    session = tvtv_session() if not TVTV_FIXTURE_DIR else None
    got, fails = {}, {}
    def job(cid):
        return cid, tvtv_station(session, mapping[cid], day_keys)
    with ThreadPoolExecutor(max_workers=TVTV_WORKERS) as ex:
        futs = [ex.submit(job, cid) for cid in mapping]
        for fu in as_completed(futs):
            try:
                cid, items = fu.result()
                got[cid] = items
            except Exception as e:
                fails[str(e)] = fails.get(str(e), 0) + 1
    if fails:
        log("tvtv: fetch errors: " + ", ".join(f"{k} x{v}" for k, v in fails.items()))
    log(f"tvtv: data for {sum(1 for v in got.values() if v)} / {len(mapping)} channels")
    return mapping, got

# ── 3. merge ──────────────────────────────────────────────────────────────────
def now_item(items):
    for it in items:
        if it["start"] <= NOW < it["stop"]: return it
    return None

def merge(chans, matt, tvtv_got):
    used, mismatch, empty = 0, [], 0
    out = {}
    for cid in chans:
        m = matt.get(cid, [])
        t = tvtv_got.get(cid)
        if not t:
            if t is not None: empty += 1
            out[cid] = m; continue
        mn, tn = now_item(m), now_item(t)
        if mn and tn and not similar(mn["title"], tn["title"]):
            # tolerate the next/previous slot matching (minor clock skew)
            near = [x for x in t if abs((x["start"] - mn["start"]).total_seconds()) <= 1800]
            if not any(similar(mn["title"], x["title"]) for x in near):
                mismatch.append(f"{chans[cid]['name']} [{cid}]: Samsung '{mn['title']}' vs Gracenote '{tn['title']}'")
                out[cid] = m; continue
        # Samsung's own data wins for as far as it goes (it has descriptions,
        # artwork and episode numbers); Gracenote continues from where it ends.
        if m:
            edge = m[-1]["stop"]
            tail = [dict(x) for x in t if x["stop"] > edge + timedelta(minutes=5)]
            if tail and tail[0]["start"] < edge:
                tail[0]["start"] = edge            # trim the straddling airing
            merged = m + tail
        else:
            merged = t
        out[cid] = merged; used += 1
    log(f"merge: {used} channels extended with Gracenote, {len(mismatch)} rejected "
        f"(now-playing mismatch), {empty} returned no airings")
    if mismatch:
        log("  rejected mappings (kept on Samsung/Matt data):")
        for x in mismatch: log("   - " + x)
    return out

# ── 4. write ──────────────────────────────────────────────────────────────────
SE_RE = re.compile(r"^\s*S(\d{1,3})\s*E(\d{1,4})\s*$", re.I)

def write_outputs(chans, progs):
    order = sorted(chans, key=lambda c: (REGIONS.index(chans[c]["region"]),
                                         chans[c]["chno"] or 99999, chans[c]["name"].lower()))
    with open(OUT_M3U, "w", encoding="utf-8") as f:
        f.write(f'#EXTM3U url-tvg="{EPG_RAW}"\n')
        for cid in order:
            c = chans[cid]
            grp = f'{REGION_LABEL[c["region"]]} - {c["group"]}'
            chno = f' tvg-chno="{c["chno"]}"' if c["chno"] else ""
            f.write(f'#EXTINF:-1 tvg-id="{cid}" tvg-name="{c["name"]}" tvg-logo="{c["logo"]}"'
                    f' group-title="{grp}"{chno},{c["name"]}\n{STREAM_URL.format(id=cid)}\n')
    keep_from = NOW - timedelta(hours=KEEP_PAST_H)
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="samsung-gracenote">']
    for cid in order:
        c = chans[cid]
        lines.append(f'<channel id={quoteattr(cid)}><display-name>{escape(c["name"])}</display-name>'
                     + (f'<icon src={quoteattr(c["logo"])}/>' if c["logo"] else "") + "</channel>")
    n = 0
    for cid in order:
        for p in progs.get(cid, []):
            if p["stop"] <= keep_from: continue
            s = f'<programme start="{xmltv_ts(p["start"])}" stop="{xmltv_ts(p["stop"])}" channel={quoteattr(cid)}>'
            s += f"<title>{escape(p['title'])}</title>"
            sub, epnum = p.get("sub"), list(p.get("epnum") or [])
            m = SE_RE.match(sub or "")
            if m:                                   # Matt puts "S08 E04" in sub-title
                se, ee = int(m.group(1)), int(m.group(2))
                sub = None
                if not epnum:
                    epnum = [("onscreen", f"S{se:02d} E{ee:02d}"), ("xmltv_ns", f"{se-1}.{ee-1}.")]
            if sub: s += f"<sub-title>{escape(sub)}</sub-title>"
            if p.get("desc"): s += f"<desc>{escape(p['desc'])}</desc>"
            if p.get("icon"): s += f"<icon src={quoteattr(p['icon'])}/>"
            for sysname, txt in epnum:
                if txt:
                    if sysname == "onscreen":       # house style: spaced "S01 E05"
                        mm = re.match(r"^S(\d+)\s*E(\d+)$", txt.strip(), re.I)
                        if mm: txt = f"S{int(mm.group(1)):02d} E{int(mm.group(2)):02d}"
                    s += f'<episode-num system="{sysname or "onscreen"}">{escape(txt)}</episode-num>'
            lines.append(s + "</programme>"); n += 1
    lines.append("</tv>")
    data = ("\n".join(lines) + "\n").encode("utf-8")
    ET.fromstring(data)                              # refuse to write invalid XML
    with gzip.open(OUT_EPG, "wb", compresslevel=9) as f:
        f.write(data)
    log(f"wrote {OUT_M3U} ({len(order)} channels) and {OUT_EPG} ({n} programmes)")

def horizon_stats(chans, progs, mapping, label):
    hrs = []
    for cid in chans:
        if (cid in mapping) != (label == "mapped"): continue
        p = progs.get(cid) or []
        hrs.append(max(0, (p[-1]["stop"] - NOW).total_seconds() / 3600) if p else 0)
    if hrs:
        hrs.sort()
        log(f"  hours of guide ahead — {label:8} channels: median {hrs[len(hrs)//2]:.1f}h, "
            f"min {hrs[0]:.1f}h, max {hrs[-1]:.1f}h  (n={len(hrs)})")

def main():
    log(f"Samsung TV Plus build {NOW:%Y-%m-%d %H:%M} UTC")
    chans, matt = load_matt()
    try:
        mapping, got = load_tvtv(chans)
    except Exception as e:
        log(f"!! tvtv stage failed entirely ({e}) — writing Samsung/Matt-only guide")
        mapping, got = {}, {}
    progs = merge(chans, matt, got)
    write_outputs(chans, progs)
    horizon_stats(chans, progs, mapping, "mapped")
    horizon_stats(chans, progs, mapping, "unmapped")
    with open(OUT_REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")

if __name__ == "__main__":
    main()
