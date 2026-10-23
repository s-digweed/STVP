#!/usr/bin/env python3
"""
Samsung TV Plus — extended EPG (all regions Matt publishes; Gracenote extension for US).

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

Outputs: samsung.xml.gz, samsung_report.txt
"""
import csv, gzip, json, os, re, sys, time, unicodedata, difflib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr
import xml.etree.ElementTree as ET

import requests

# ── config ────────────────────────────────────────────────────────────────────
# The EPG covers EVERY region Matt publishes (us ca gb de fr es it at ch in kr …),
# DRM channels included, so other playlists using Samsung tvg-ids get a guide too.
TVTV_DAYS   = int(os.getenv("TVTV_DAYS", "3"))   # UTC days fetched: today + N-1
TVTV_WORKERS = 6
KEEP_PAST_H = 3                                  # keep this many hours of past guide

MATT_CHANNELS = "https://i.mjh.nz/SamsungTVPlus/.channels.json.gz"
MATT_EPG      = "https://i.mjh.nz/SamsungTVPlus/all.xml.gz"
TVTV_BASE     = "https://tvtv.us"
MAP_FILE      = "samsung_gracenote_map.csv"
OUT_EPG, OUT_REPORT = "samsung.xml.gz", "samsung_report.txt"


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
    s = re.sub(r"['\u2019\u2018`]", "", s)          # America's -> americas (keeps acronyms intact)
    s = re.sub(r"\b(the|a|an)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()

def similar(a, b):
    a, b = norm(a), norm(b)
    if not a or not b: return False
    if a == b: return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 5 and short in long_: return True
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
    chans = {}
    for rg, R in data["regions"].items():
        for cid, c in R.get("channels", {}).items():
            chans[cid] = {"name": c["name"].strip(), "logo": c.get("logo", ""),
                          "group": c.get("group", "Other"), "chno": c.get("chno"), "region": rg,
                          "drm": bool(c.get("license_url"))}
    progs = {cid: [] for cid in chans}
    try:
        root = ET.fromstring(get_gz(MATT_EPG, MATT_EPG_FILE))
    except Exception as e:
        log(f"!! Matt EPG download failed: {e}"); root = ET.fromstring("<tv/>")
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
    regs = sorted({c["region"] for c in chans.values()})
    log(f"Matt: {len(chans)} channels in {len(regs)} regions ({' '.join(regs)}), "
        f"{sum(map(len, progs.values()))} programmes")
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
        title = (title or "").strip()
        if sub and title != sub and title.endswith(" " + sub):     # tvtv glues the
            title = title[: -len(sub) - 1].strip()                 # episode onto the title
        cat = None
        my = re.search(r"\s*\((\d{4}),\s*([^)]+)\)\s*$", title)  # "Movie (2013, Science fiction)"
        if my:
            title = f"{title[:my.start()].strip()} ({my.group(1)})"
            cat = my.group(2).strip()
        out.append({"start": start, "stop": start + timedelta(minutes=max(mins, 1)),
                    "title": title or "Unknown", "sub": sub or None, "cat": cat})
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
GENERIC_RE = re.compile(r"\b(to be announced|tba|programming|off air|sign off|paid programming)\b", re.I)
STOP = {"and", "with", "the", "of", "in", "on", "for", "vs", "v", "live", "new", "show", "tv"}

def now_item(items):
    for it in items:
        if it["start"] <= NOW < it["stop"]: return it
    return None

def tokens(s):
    return {w for w in norm(s).split() if w not in STOP and (len(w) > 2 or w.isdigit())}

def acronym(s):
    return "".join(w[0] for w in norm(s).split() if w)

def abbrev_of(short, initials):
    """AFV ~ America's Funniest Home Videos: same first/last initial, letters in
    order, at most one word skipped."""
    if len(short) < 3 or not initials or len(initials) - len(short) not in (0, 1): return False
    if short[0] != initials[0] or short[-1] != initials[-1]: return False
    it = iter(initials)
    return all(c in it for c in short)

def title_match(a, b):
    """a = Samsung programme, b = Gracenote airing. Titles are often worded
    differently between the two, so compare titles AND sub-titles, abbreviations
    (AFV = America's Funniest Home Videos) and shared keywords."""
    for x in (a["title"], a.get("sub")):
        for y in (b["title"], b.get("sub")):
            if x and y and similar(x, y): return True
    sa = norm(a["title"]).replace(" ", "")
    for full in (b["title"], b.get("sub") or ""):
        if abbrev_of(sa, acronym(full)): return True
    A = tokens(f'{a["title"]} {a.get("sub") or ""}')
    B = tokens(f'{b["title"]} {b.get("sub") or ""}')
    if A and B:
        shared = len(A & B)
        if shared >= 2 and shared / min(len(A), len(B)) >= 0.5: return True
    return False

def is_generic(t, name):
    """Gracenote sometimes only has a placeholder (channel name / 'To Be Announced')."""
    win = [x for x in t if NOW - timedelta(hours=1) <= x["start"] < NOW + timedelta(hours=24)]
    if not win: return True
    bad = sum(1 for x in win if GENERIC_RE.search(x["title"])
              or (similar(x["title"], name) and not x.get("sub")))
    return bad / len(win) >= 0.6

def compare(m, t):
    """Score how well a Gracenote schedule matches Samsung's over their overlap
    (now-1h .. end of Samsung data): share of Samsung programmes with a Gracenote
    airing starting within 3 min (time), and of those, how many titles agree."""
    if not m: return None
    sam = [x for x in m if NOW - timedelta(hours=1) <= x["start"] < m[-1]["stop"]]
    if len(sam) < 2: return None
    timed = titled = 0
    # share of Samsung starts that are "irregular" (not on a :00/:30 grid) — only
    # those make a time-alignment match meaningful (hourly news grids all align)
    odd = sum(1 for x in sam if x["start"].minute % 30 or x["start"].second) / len(sam)
    for x in sam:
        near = [y for y in t if abs((y["start"] - x["start"]).total_seconds()) <= 180]
        if near:
            timed += 1
            if any(title_match(x, y) for y in near): titled += 1
    return timed / len(sam), titled / len(sam), len(sam), odd

def accept(m, t, name):
    sc = compare(m, t)
    mn, tn = now_item(m), now_item(t)
    if sc:
        timed, titled, n, odd = sc
        if titled >= 0.4 or (timed >= 0.75 and odd >= 0.5):
            return True, f"{timed:.0%} start times / {titled:.0%} titles agree over {n} programmes"
    if mn and tn and title_match(mn, tn):
        return True, "now-playing agrees"
    # Single-show / loop channels (Degrassi, Duck Dynasty, Stingray music…): Gracenote
    # lists the channel's own name as the programme — that CONFIRMS the mapping.
    win = [x for x in t if NOW - timedelta(hours=1) <= x["start"] < NOW + timedelta(hours=24)]
    if win and sum(1 for x in win if similar(x["title"], name)) / len(win) >= 0.6:
        return True, "single-show channel (Gracenote lists the channel's own name)"
    if is_generic(t, name):
        return False, "Gracenote has only placeholder listings (e.g. 'To Be Announced')"
    if mn and tn:
        return False, (f"Samsung '{mn['title']}' vs Gracenote '{tn['title']}'"
                       + (f" ({sc[0]:.0%} times / {sc[1]:.0%} titles agree)" if sc else ""))
    if not m: return True, "no Samsung guide to compare — Gracenote used as-is"
    return False, "too little overlap to verify"

def splice(m, t):
    """Samsung's own data wins for as far as it goes (descriptions, artwork,
    episode numbers); Gracenote continues from where it ends."""
    if not m: return [dict(x) for x in t]
    edge = m[-1]["stop"]
    tail = [dict(x) for x in t if x["stop"] > edge + timedelta(minutes=5)]
    if tail and tail[0]["start"] < edge:
        tail[0]["start"] = edge                    # trim the straddling airing
    return m + tail

def merge(chans, matt, tvtv_got, mapping):
    out, used, rejected, remapped, empty = {}, 0, [], [], 0
    pool = {cid: t for cid, t in tvtv_got.items()
            if t and not is_generic(t, chans[cid]["name"])}
    for cid in chans:
        m, t = matt.get(cid, []), tvtv_got.get(cid)
        if not t:
            if t is not None: empty += 1
            out[cid] = m; continue
        ok, why = accept(m, t, chans[cid]["name"])
        if not ok and m:
            # wrong/swapped mapping? look for the station that really matches
            best = None
            for oc, ot in pool.items():
                if oc == cid: continue
                sc = compare(m, ot)
                if sc and sc[1] >= 0.5 and sc[0] >= 0.6 and (not best or sc[1] > best[1][1]):
                    best = (oc, sc)
            if best:
                oc, sc = best
                t = ot = pool[oc]
                remapped.append(f"{chans[cid]['name']} [{cid}] -> using station {mapping.get(oc)} "
                                f"(listed for {chans[oc]['name']}; {sc[0]:.0%} times / {sc[1]:.0%} "
                                f"titles agree). To make it permanent, change {cid}'s tmsid from "
                                f"{mapping.get(cid)} to {mapping.get(oc)} in samsung_gracenote_map.csv")
                ok = True
        if not ok:
            rejected.append(f"{chans[cid]['name']} [{cid}]: {why}")
            out[cid] = m; continue
        out[cid] = splice(m, t); used += 1
    log(f"merge: {used} channels extended with Gracenote, {len(remapped)} auto-remapped, "
        f"{len(rejected)} kept on Samsung data, {empty} returned no airings")
    if remapped:
        log("  auto-remapped (mapping looked wrong, matching station found):")
        for x in remapped: log("   - " + x)
    if rejected:
        log("  kept on Samsung/Matt data:")
        for x in rejected: log("   - " + x)
    return out

# ── 4. write ──────────────────────────────────────────────────────────────────
SE_RE = re.compile(r"^\s*S(\d{1,3})\s*E(\d{1,4})\s*$", re.I)

def write_outputs(chans, progs):
    order = sorted(chans, key=lambda c: (chans[c]["region"], chans[c]["chno"] or 99999,
                                         chans[c]["name"].lower()))
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
            # normalise episode numbering: one spaced onscreen "S01 E05" + xmltv_ns,
            # dropping nonsense seasons (Stingray feeds carry e.g. "S470000 E02")
            se_pair = None
            for sysname, txt in epnum:
                mm = re.match(r"^\s*S(\d+)\s*E(\d+)\s*$", txt or "", re.I)
                if mm: se_pair = (int(mm.group(1)), int(mm.group(2))); break
            if not se_pair:
                for sysname, txt in epnum:
                    mm = re.match(r"^\s*(\d+)\s*\.\s*(\d+)\s*\.", txt or "")
                    if sysname == "xmltv_ns" and mm:
                        se_pair = (int(mm.group(1)) + 1, int(mm.group(2)) + 1); break
            if sub and norm(sub) != norm(p["title"]): s += f"<sub-title>{escape(sub)}</sub-title>"
            if p.get("desc"): s += f"<desc>{escape(p['desc'])}</desc>"
            if p.get("cat"): s += f"<category>{escape(p['cat'])}</category>"
            if p.get("icon"): s += f"<icon src={quoteattr(p['icon'])}/>"
            if se_pair and 0 < se_pair[0] <= 100 and 0 < se_pair[1] <= 9999:
                se, ee = se_pair
                s += f'<episode-num system="onscreen">S{se:02d} E{ee:02d}</episode-num>'
                s += f'<episode-num system="xmltv_ns">{se-1}.{ee-1}.</episode-num>'
            lines.append(s + "</programme>"); n += 1
    lines.append("</tv>")
    data = ("\n".join(lines) + "\n").encode("utf-8")
    ET.fromstring(data)                              # refuse to write invalid XML
    with gzip.open(OUT_EPG, "wb", compresslevel=9) as f:
        f.write(data)
    log(f"wrote {OUT_EPG} ({len(order)} channels, all regions, {n} programmes)")

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
    progs = merge(chans, matt, got, mapping)
    write_outputs(chans, progs)
    horizon_stats(chans, progs, mapping, "mapped")
    horizon_stats(chans, progs, mapping, "unmapped")
    with open(OUT_REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")

if __name__ == "__main__":
    main()
