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

GROQ_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODELS = [m for m in os.getenv("GROQ_MODELS", "openai/gpt-oss-120b,qwen/qwen3.8-27b").split(",") if m]
SYS_PROMPT = """You are the senior editor of a professional Persian-language Telegram news channel (style: BBC Persian / Iran International). Rewrite the English news item below into fluent, natural, journalistic Persian. Use ONLY facts present in the input; never add details, dates, casualties, causes, context or opinion that are not in the input. Keep all numbers and names exact; transliterate foreign names in standard Persian press spelling (e.g. منچستر یونایتد، کریستین لاگارد، ولادیمیر پوتین). Use correct Persian orthography with zero-width non-joiners (می‌شود، دست‌کم).
Return ONLY a JSON object:
{"headline": "short punchy Persian headline, max ~14 words, journalistic, not a literal translation",
 "body": "1-2 fluent Persian sentences with the key facts from the input",
 "urgent": true only for genuinely major breaking news (a military attack on Iran, major war escalation, a major central bank rate decision by the Fed/ECB, death of a head of state, etc.), otherwise false,
 "source_fa": "Persian name of the source if well known (رویترز، بی‌بی‌سی، الجزیره، گاردین، ایران اینترنشنال، اسکای اسپورتس، ...), otherwise the original name"}"""

def fa(s): return bool(re.search(r"[\u0600-\u06FF]", s or ""))

def llm_rewrite(title, summary, source, category):
    if not GROQ_KEY: return None
    user = json.dumps({"category": category, "source": source, "title": title, "summary": summary}, ensure_ascii=False)
    for model in GROQ_MODELS:
        for attempt in range(3):
            body = {"model": model, "temperature": 0.2, "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": SYS_PROMPT}, {"role": "user", "content": user}]}
            if "gpt-oss" in model: body["reasoning_effort"] = "low"
            try:
                r = requests.post("https://api.groq.com/openai/v1/chat/completions", json=body, timeout=60,
                                  headers={"Authorization": "Bearer " + GROQ_KEY})
            except Exception as ex:
                print("groq error", model, type(ex).__name__); break
            if r.status_code == 429:
                wait = min(float(r.headers.get("retry-after", 5) or 5), 20)
                print(f"groq 429 {model}, wait {wait}s"); time.sleep(wait); continue
            if not r.ok:
                print("groq http", model, r.status_code, r.text[:150]); break
            try:
                txt = r.json()["choices"][0]["message"]["content"]
                m = re.search(r"\{.*\}", txt, re.S)
                d = json.loads(m.group(0) if m else txt)
                h, b = str(d.get("headline", "")).strip(), str(d.get("body", "")).strip()
                if fa(h):
                    return {"headline": h, "body": b if fa(b) else "", "urgent": d.get("urgent") in (True, "true", "True"),
                            "source_fa": str(d.get("source_fa") or source).strip(), "model": model}
                print("groq bad output", model)
            except Exception as ex:
                print("groq parse fail", model, type(ex).__name__)
            break
    return None

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

def send(msg, preview=False):
    if DRY: print(msg + "\n" + "-" * 40); return True
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data={
        "chat_id": CHANNEL, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": "false" if preview else "true"}, timeout=30)
    if r.status_code == 429:
        time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1); return send(msg, preview)
    if not r.ok: print("telegram error:", r.text)
    return r.ok

def main():
    if not DRY and not (TOKEN and CHANNEL): raise SystemExit("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID")
    cfg = yaml.safe_load(open("feeds.yaml", encoding="utf-8"))
    cats = cfg["categories"]
    sig = cfg.get("channel_signature", ""); preview = bool(cfg.get("link_preview", False))
    global STATE
    STATE = os.getenv("STATE_FILE", STATE)
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
        rule = cats[cat]; label = rule.get("label", "خبر").replace("_", " ")
        res = llm_rewrite(raw_t, raw_s, src, label)
        if res:
            title, summ, urgent, src_fa = res["headline"], res["body"], res["urgent"], res["source_fa"]
            print("llm:", res["model"])
        else:  # fallback: literal translation
            title = tr(raw_t); urgent = False; src_fa = src
            summ = tr(raw_s) if len(raw_s) > 25 else ""
            if summ and not fa(summ): summ = ""
        if not fa(title):  # never post English; retry next run
            print("skip untranslated:", raw_t[:60])
            if i in seen: seen.remove(i)
            continue
        head = f"🔴 فوری | {label}" if urgent else f"{rule.get('emoji','📰')} {label}"
        msg = f"{head}\n\n<b>{html.escape(title, quote=False)}</b>\n"
        if summ: msg += f"\n{html.escape(summ, quote=False)}\n"
        msg += f"\n🔗 <a href=\"{html.escape(e.get('link',''), quote=True)}\">منبع: {html.escape(src_fa, quote=False)}</a>"
        if sig: msg += f"\n🆔 {html.escape(sig, quote=False)}"
        if cfg.get("hashtags", True): msg += f"\n\n#{label.replace(' ', '_')}"
        if send(msg, preview): seen.append(tk)
        time.sleep(0 if DRY else 3)
    if not DRY:
        os.makedirs("state", exist_ok=True)
        json.dump({"seen": seen[-CAP:]}, open(STATE, "w"))
    print("done")

if __name__ == "__main__": main()
