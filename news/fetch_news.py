#!/usr/bin/env python3
"""Builds docs/news.json from public RSS/Atom feeds, following the written rules
(sections A-I of the project document). Standard library only.

What it does NOT do: translate, summarise, rewrite or add facts. It only selects.
Selection is heuristic (it sees title, summary, date, source), and the report says so.
"""
import json, os, re, sys, html, datetime as dt, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = os.path.join(ROOT, "news", "sources.json")
OUT = os.path.join(ROOT, "docs", "news.json")
REPORT = os.path.join(ROOT, "docs", "report.json")
HISTORY = os.path.join(ROOT, "docs", "history.json")
FIXTURE_DIR = os.environ.get("FIXTURE_DIR")      # tests only: <source id>.xml files instead of the network
NOW = dt.datetime.now(dt.timezone.utc)

# ---- Rules (section C, E) ----
THRESHOLD = 5            # minimum score out of 10
HIGH = 8                 # "high weight": may exceed group limits
BASE = 10                # normal size of the set
MAX_TOTAL = 14           # hard ceiling when many items are high weight
GROUP_CAPS = {"alt": 3, "tech": 2, "policy": 2, "craft": 1, "arch": 2}
PER_SOURCE = 2
REPEAT_DAYS = 30
SAME_STORY = 0.35        # title word overlap above which two items are treated as the same story
FORCE = os.environ.get("FORCE") == "1"   # manual re-run: rebuild today's set
TIER_LABEL = {1: "Primary", 2: "Editorial", 3: "Specialist"}

def rx(words): return re.compile(r"(?<![a-z0-9])(?:" + "|".join(words) + r")", re.I)

GROUP_RX = [  # first match wins; most specific first
    ("craft", rx([r"masonry", r"stove", r"kiln", r"chimney", r"lime plaster", r"timber[- ]frame", r"traditional building", r"craftsm", r"artisan", r"thatch", r"woodwork"])),
    ("arch",  rx([r"archaeolog", r"archeolog", r"excavat", r"shipwreck", r"\bwreck", r"ancient", r"medieval", r"roman\b", r"neolithic", r"bronze age", r"pyramid", r"\btomb", r"manuscript", r"radiocarbon", r"titanic", r"artefact", r"artifact"])),
    ("alt",   rx([r"hemp", r"bio-?based", r"biomaterial", r"bioplastic", r"natural fib", r"renewable", r"\bsolar\b", r"wind (?:power|farm|turbine|energy)", r"geothermal", r"circular economy", r"recycl", r"\breuse", r"bamboo", r"mycelium", r"green hydrogen", r"heat pump", r"low-carbon (?:material|concrete|cement)", r"timber"])),
    ("tech",  rx([r"quantum", r"robot", r"semiconductor", r"\bchips?\b", r"artificial intelligence", r"\bai\b", r"machine learning", r"\bbatter", r"\bfusion\b", r"nanotech", r"3d[- ]print", r"supercomputer", r"open-?source", r"crispr", r"gene editing"])),
    ("policy", rx([r"\blaws?\b", r"legislation", r"regulation", r"treaty", r"agreement", r"\bpolicy", r"parliament", r"directive", r"\bban on", r"subsid", r"carbon (?:price|tax|market)", r"ecodesign", r"court ruled", r"\bruling", r"protected area", r"conservation (?:initiative|plan)", r"\bfunding\b", r"\binitiative"])),
]
CONFLICT = rx([r"war\b", r"wars\b", r"warfare", r"invasion", r"air ?strike", r"bombing", r"bombed", r"missile", r"shelling", r"killed", r"killing", r"massacre", r"genocide", r"hostage", r"terror", r"attacks?\b", r"attacked", r"casualt", r"soldier", r"troops", r"ceasefire", r"\bgaza", r"hamas", r"militant", r"\bcoup\b"])
ALARM = rx([r"shocking", r"terrifying", r"horrifying", r"doomsday", r"apocalyp", r"collapse", r"catastroph", r"devastat", r"death toll", r"\bdeadly", r"\bdies\b", r"\bdied\b", r"fatal"])
MARKET = rx([r"sponsored", r"advertorial", r"partner content", r"promoted", r"press release", r"paid post", r"buy now", r"order now", r"discount", r"\d+% off", r"\bsale\b", r"\bpricing", r"free trial", r"coupon", r"promo code", r"giveaway", r"webinar", r"subscribe now"])
SUPER = rx([r"revolutionary", r"game-?changing", r"miracle", r"breakthrough", r"unbelievable"])
HEMP_OUT = rx([r"\bcbd\b", r"cannabis", r"marijuana", r"\bweed\b", r"\bthc\b", r"dispensar", r"recreational", r"legali[sz]", r"psychedelic"])
HEMP_OK = rx([r"industrial hemp", r"hempcrete", r"hemp (?:fib|fab|build|insul|plastic|concrete|textile|crop|seed|construction|material)"])
FRINGE = rx([r"ancient aliens", r"lost civili[sz]ation", r"don.t want you to know", r"secret (?:of|history)", r"forbidden", r"suppressed", r"hidden history", r"mysterious", r"unexplained", r"anunnaki", r"atlantis"])

EVID_STRONG = rx([r"peer-?reviewed", r"published in", r"\bstud(?:y|ies)\b", r"researchers", r"scientists", r"\bjournal\b", r"\breport\b", r"\bdata\b", r"analysis", r"survey", r"court", r"directive", r"regulation", r"agreement", r"radiocarbon", r"dating", r"excavation", r"\blaw\b"])
EVID_MED = rx([r"expert", r"according to", r"\bsaid\b", r"interview", r"officials", r"announced"])
EVID_WEAK = rx([r"plans to", r"will launch", r"unveils", r"opinion", r"op-ed", r"\bwhy\b", r"\bshould\b"])
SCALE_BIG = rx([r"global", r"worldwide", r"countries", r"international", r"\beu\b", r"europe", r"united nations", r"million", r"billion"])
SCALE_MID = rx([r"national", r"nationwide", r"country", r"\bstate\b", r"province", r"region", r"\bcity\b"])
STOP = set("with from that this have will over into more than their about after could would which while there these those been were they them what when your also".split())

def log(*a): print(*a, flush=True)

def clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()

def local(tag): return tag.rsplit("}", 1)[-1]

def child_text(el, names):
    for c in el:
        if local(c.tag) in names and (c.text or "").strip():
            return c.text.strip()
    return ""

def parse_date(s):
    s = (s or "").strip()
    if not s: return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try: d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception: return None
    if d.tzinfo is None: d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)

def parse_feed(raw):
    root = ET.fromstring(raw)
    entries = [e for e in root.iter() if local(e.tag) in ("item", "entry")]
    out = []
    for e in entries:
        title = clean(child_text(e, ("title",)))
        link = ""
        for c in e:
            if local(c.tag) == "link":
                if c.get("href") and c.get("rel", "alternate") == "alternate": link = c.get("href"); break
                if (c.text or "").strip(): link = c.text.strip(); break
        date = parse_date(child_text(e, ("pubDate", "published", "updated", "date")))
        summary = clean(child_text(e, ("description", "summary", "encoded", "content")))
        cats = " ".join(clean(c.text) for c in e if local(c.tag) in ("category", "subject") and c.text)
        out.append({"title": title, "link": link, "date": date, "summary": summary, "cats": cats})
    return out

def fetch(src):
    if FIXTURE_DIR:
        p = os.path.join(FIXTURE_DIR, src["id"] + ".xml")
        if not os.path.exists(p): raise RuntimeError("no fixture")
        return open(p, "rb").read()
    req = urllib.request.Request(src["url"], headers={"User-Agent": "cosmic-gift-news/1.0", "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()

def toks(s):
    return {w for w in re.findall(r"[a-z]{4,}", s.lower()) if w not in STOP}

def jacc(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0

def classify(c):
    text = " ".join([c["title"], c["summary"], c["cats"]])
    for name, r in GROUP_RX:
        if r.search(text): return name
    return None

def exclude_reason(c):
    text = " ".join([c["title"], c["summary"], c["cats"]])
    if CONFLICT.search(text): return "conflict"
    if ALARM.search(text): return "alarm"
    if MARKET.search(text): return "marketing"
    if SUPER.search(c["title"]): return "superlative"
    if FRINGE.search(text): return "fringe"
    if HEMP_OUT.search(text) and not HEMP_OK.search(text): return "hemp_out_of_scope"
    if re.search(r"[A-Z]{5,}.*[A-Z]{5,}", c["title"]): return "shouting"
    return None

def score(c, all_c):
    text = " ".join([c["title"], c["summary"]])
    parts = {}
    if EVID_WEAK.search(text): parts["evidence"] = 0
    elif EVID_STRONG.search(text): parts["evidence"] = 2
    elif EVID_MED.search(text): parts["evidence"] = 1
    else: parts["evidence"] = 0
    parts["scale"] = 2 if SCALE_BIG.search(text) else (1 if SCALE_MID.search(text) else 0)
    age = (NOW - c["date"]).days if c["date"] else 999
    parts["recency"] = 2 if age <= 3 else (1 if age <= 14 else 0)
    mine = toks(c["title"])
    conf = [o["source"] for o in all_c if o["source_id"] != c["source_id"] and jacc(mine, toks(o["title"])) >= SAME_STORY]
    parts["confirmation"] = 2 if conf else (1 if c["tier"] == 1 else 0)
    words = re.findall(r"\b[A-Z][a-z]{2,}", " ".join(text.split()[1:]))
    has_num = bool(re.search(r"\d", text))
    names = len(set(words))
    parts["concreteness"] = 2 if (has_num and names >= 2) else (1 if (has_num or names >= 2) else 0)
    return sum(parts.values()), parts, sorted(set(conf))

def snippet(c, allowed):
    if not allowed or not c["summary"]: return ""
    s = c["summary"]
    if len(s) <= 220: return s
    return s[:220].rsplit(" ", 1)[0].rstrip(",;: ") + "…"

def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    srcs = json.load(open(SOURCES))["sources"]
    if os.path.exists(OUT) and not FORCE:
        try:
            if json.load(open(OUT)).get("generated_at", "")[:10] == NOW.date().isoformat():
                log("news.json was already built today; leaving it as is (set FORCE=1 to rebuild)"); return
        except Exception: pass
    report = {"generated_at": NOW.isoformat(timespec="seconds"), "sources": {}, "dropped": {}, "note": "Selection is heuristic: it reads only title, summary, date and source."}
    drop = report["dropped"]
    def dropped(r): drop[r] = drop.get(r, 0) + 1
    cands = []
    for s in srcs:
        try:
            items = parse_feed(fetch(s))
            if not items: raise ValueError("readable but no items")
            report["sources"][s["id"]] = {"status": "ok", "items": len(items)}
            log("OK    %-14s %3d items  %s" % (s["id"], len(items), s["url"]))
        except urllib.error.HTTPError as e:
            report["sources"][s["id"]] = {"status": "failed", "reason": "HTTP %s" % e.code}
            log("FAIL  %-14s HTTP %s  %s" % (s["id"], e.code, s["url"])); continue
        except Exception as e:
            report["sources"][s["id"]] = {"status": "failed", "reason": "%s: %s" % (type(e).__name__, e)}
            log("FAIL  %-14s %s: %s  %s" % (s["id"], type(e).__name__, e, s["url"])); continue
        for it in items:
            if not it["title"] or not it["link"].startswith("http"): dropped("no_title_or_link"); continue
            it.update({"source": s["name"], "source_id": s["id"], "tier": s["tier"], "snippet_allowed": s.get("snippet_allowed", False)})
            cands.append(it)

    hist = {}
    if os.path.exists(HISTORY):
        try: hist = json.load(open(HISTORY))
        except Exception: hist = {}
    cutoff = (NOW - dt.timedelta(days=REPEAT_DAYS)).date().isoformat()
    today = NOW.date().isoformat()
    hist = {u: d for u, d in hist.items() if d >= cutoff and not (FORCE and d == today)}

    pool = []
    for c in cands:
        if c["link"] in hist: dropped("repeated"); continue
        r = exclude_reason(c)
        if r: dropped(r); continue
        g = classify(c)
        if not g: dropped("no_topic_match"); continue
        c["group"] = g
        pool.append(c)
    for c in pool:
        c["score"], c["parts"], c["confirmed_by"] = score(c, pool)
    # near-duplicates: keep the best-scored one
    pool.sort(key=lambda c: (-c["score"], -(c["date"].timestamp() if c["date"] else 0)))
    uniq = []
    for c in pool:
        if any(jacc(toks(c["title"]), toks(u["title"])) >= SAME_STORY for u in uniq): dropped("near_duplicate"); continue
        uniq.append(c)
    elig = [c for c in uniq if c["score"] >= THRESHOLD]
    drop["below_threshold"] = len(uniq) - len(elig)

    chosen, gc, sc = [], {}, {}
    def add(c): chosen.append(c); gc[c["group"]] = gc.get(c["group"], 0) + 1; sc[c["source_id"]] = sc.get(c["source_id"], 0) + 1
    for c in elig:                                   # pass 1: respect group caps
        if len(chosen) >= BASE: break
        if gc.get(c["group"], 0) < GROUP_CAPS[c["group"]] and sc.get(c["source_id"], 0) < PER_SOURCE: add(c)
    for c in elig:                                   # pass 2: fill to BASE ignoring group caps
        if len(chosen) >= BASE: break
        if c not in chosen and sc.get(c["source_id"], 0) < PER_SOURCE: add(c)
    for c in elig:                                   # pass 3: high-weight extras up to MAX_TOTAL
        if len(chosen) >= MAX_TOTAL: break
        if c not in chosen and c["score"] >= HIGH and sc.get(c["source_id"], 0) < PER_SOURCE + 1: add(c)

    items = [{"title": c["title"], "source": c["source"], "tier": TIER_LABEL[c["tier"]], "group": c["group"],
              "date": c["date"].date().isoformat() if c["date"] else "", "link": c["link"],
              "snippet": snippet(c, c["snippet_allowed"]), "score": c["score"]} for c in chosen]
    report["chosen"] = [{"title": c["title"], "source_id": c["source_id"], "group": c["group"], "score": c["score"], "parts": c["parts"], "confirmed_by": c["confirmed_by"]} for c in chosen]
    report["counts"] = {"candidates": len(cands), "after_filters": len(pool), "eligible": len(elig), "chosen": len(chosen)}
    json.dump(report, open(REPORT, "w"), indent=1, ensure_ascii=False)

    if items:
        json.dump({"generated_at": report["generated_at"], "count": len(items), "items": items}, open(OUT, "w"), indent=1, ensure_ascii=False)
        for c in chosen: hist[c["link"]] = NOW.date().isoformat()
        json.dump(hist, open(HISTORY, "w"), indent=1)
    else:
        log("WARNING: nothing chosen; previous news.json (if any) is left untouched")
    log("\nSUMMARY", json.dumps(report["counts"]), "dropped:", json.dumps(drop))
    for it in items: log("  [%s|%s|%d] %s — %s" % (it["group"], it["tier"], it["score"], it["title"], it["source"]))

if __name__ == "__main__":
    main()
