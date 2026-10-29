import calendar, os, json, re, time, html, hashlib
import feedparser, requests, yaml

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHANNEL = os.getenv("TELEGRAM_CHANNEL_ID", "")
DRY = os.getenv("DRY_RUN", "0") == "1"
STATE = "state/seen.json"; CAP = 5000

def tr(text):
    if os.getenv("NO_TRANSLATE") == "1": return (text or "").strip()
    text = (text or "").strip()[:1500]
    if not text: return ""
    try:  # 1) free Google endpoint (no key)
        r = requests.get("https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "fa", "dt": "t", "q": text}, timeout=20)
        r.raise_for_status()
        out = "".join(p[0] for p in r.json()[0] if p[0])
        if out: return out
    except Exception as e:
        print("google fail:", str(e)[:80])
    try:  # 2) MyMemory free (max ~500 chars)
        r = requests.get("https://api.mymemory.translated.net/get",
            params={"q": text[:480], "langpair": "en|fa", "de": os.getenv("MYMEMORY_EMAIL", "")}, timeout=20)
        out = r.json()["responseData"]["translatedText"]
        if out and "MYMEMORY WARNING" not in out: return html.unescape(out)
    except Exception as e:
        print("mymemory fail:", str(e)[:80])
    return text  # 3) fallback: original text

def clean(s):
    s = re.sub(r"<[^>]+>", " ", s or ""); s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:280].rsplit(" ", 1)[0] + "…" if len(s) > 280 else s

def uid(e):
    return hashlib.sha1((e.get("id") or e.get("link") or e.get("title", "")).encode()).hexdigest()[:16]

def tkey(t):  # same headline from different feeds -> one post
    t = html.unescape(t or "").strip(); t = (t.rpartition(" - ")[0] or t).lower()
    return "t" + hashlib.sha1(re.sub(r"[^a-z0-9]", "", t).encode()).hexdigest()[:15]

def has(text, words):
    return any(re.search(r"(?<![a-z])" + re.escape(w.lower()) + r"(?![a-z])", text) for w in words or [])

def classify(text, feed, cats):
    text = text.lower()
    for c in feed["categories"]:
        rule = cats[c]
        if has(text, rule.get("exclude")): continue
        if c == "transfer" or feed.get("filter", True) or c != feed["categories"][-1]:
            if not has(text, rule.get("include")): continue
            if rule.get("require_also") and not has(text, rule["require_also"]): continue
        return c
    return None

def send(msg):
    if DRY: print(msg + "\n" + "-" * 40); return True
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data={
        "chat_id": CHANNEL, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": "false"}, timeout=30)
    if r.status_code == 429:
        time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1); return send(msg)
    if not r.ok: print("telegram error:", r.text)
    return r.ok

def main():
    if not DRY and not (TOKEN and CHANNEL): raise SystemExit("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID")
    cfg = yaml.safe_load(open("feeds.yaml", encoding="utf-8"))
    cats = cfg["categories"]
    MAX_POSTS = int(os.getenv("MAX_POSTS", cfg.get("max_posts_per_run", 8)))
    FIRST = int(os.getenv("FIRST_RUN_POSTS", cfg.get("first_run_posts", 4)))
    max_age = float(cfg.get("max_age_hours", 6)) * 3600
    try: seen = json.load(open(STATE))["seen"]
    except Exception: seen = []
    first = len(seen) == 0; seenset = set(seen); new = []; batch = set(); now = time.time()
    for f in cfg["feeds"]:
        try:
            d = feedparser.parse(requests.get(f["url"], timeout=20, headers={"User-Agent": "Mozilla/5.0"}).content)
        except Exception as ex:
            print("feed fail", f["name"], ex); continue
        for e in d.entries[:30]:
            i, tk = uid(e), tkey(e.get("title", ""))
            if i in seenset or tk in seenset or tk in batch: continue
            pt = e.get("published_parsed") or e.get("updated_parsed")
            ts = calendar.timegm(pt) if pt else now
            seen.append(i); seenset.add(i)  # mark everything we looked at
            if now - ts > max_age: continue
            cat = classify(e.get("title", "") + " " + clean(e.get("summary", "")), f, cats)
            if not cat: continue
            batch.add(tk); new.append((cats[cat].get("priority", 9), -ts, i, tk, cat, f, e))
    new.sort(key=lambda x: (x[0], x[1]))  # priority first, then newest
    to_post = new[:FIRST if first else MAX_POSTS]
    print(f"candidates: {len(new)} ->", {c: sum(1 for x in new if x[4] == c) for c in cats})
    for _, _, i, tk, cat, f, e in to_post:
        raw_t = clean(e.get("title", "")); raw_s = clean(e.get("summary", ""))
        src = f["name"]
        if "news.google.com" in f["url"]:
            if " - " in raw_t:
                raw_t, _, src = raw_t.rpartition(" - ")
            raw_s = ""  # Google News summary only repeats the title
        if raw_s.lower().startswith(raw_t.lower()[:40]): raw_s = raw_s[len(raw_t):].strip()
        title = tr(raw_t); summ = tr(raw_s) if len(raw_s) > 25 else ""
        rule = cats[cat]
        msg = f"{rule.get('emoji','📰')} #{rule.get('label','خبر').replace(' ','_')}\n\n<b>{html.escape(title, quote=False)}</b>\n"
        if summ: msg += f"\n{html.escape(summ, quote=False)}\n"
        msg += f"\n🔗 منبع: <a href=\"{html.escape(e.get('link',''), quote=True)}\">{html.escape(src, quote=False)}</a>"
        if send(msg): seen.append(tk)
        time.sleep(0 if DRY else 3)
    if not DRY:
        os.makedirs("state", exist_ok=True)
        json.dump({"seen": seen[-CAP:]}, open(STATE, "w"))
    print(f"done: posted {len(to_post)}")

if __name__ == "__main__": main()
