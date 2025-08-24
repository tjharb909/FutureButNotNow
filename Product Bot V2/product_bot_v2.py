# product_bot_v2.py
import os, sys, csv, json, re, random, urllib.parse
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple

import tweepy  # v2 client + v1.1 API for media
from openai import OpenAI

# Local utils (Slack)
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "utils"))
from slack_notifier import notify_slack  # noqa

# ---------- CONFIG ----------
OPENAI_API_KEY           = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL             = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
X_API_KEY                = os.getenv("TWITTER_API_KEY")
X_API_SECRET             = os.getenv("TWITTER_API_SECRET")
X_ACCESS_TOKEN           = os.getenv("TWITTER_ACCESS_TOKEN")
X_ACCESS_SECRET          = os.getenv("TWITTER_ACCESS_SECRET")

AFFILIATE_TAG            = os.getenv("AFFILIATE_TAG", "futurebutnotn-20")
TRACKING_IDS_BY_MODE     = json.loads(os.getenv("TRACKING_IDS_BY_MODE", "{}"))  # e.g. {"spiky":"futurebutnotn-20","confession":"futurebutnotn-21",...}

ROOT                     = os.path.dirname(os.path.abspath(__file__))
PRODUCT_CSV              = os.path.join(ROOT, "products.csv")
IMAGES_DIR               = os.path.join(ROOT, "images")

LOG_DIR                  = os.path.join(ROOT, "logs")
TWEET_LOG_CSV            = os.path.join(LOG_DIR, "tweet_logs.csv")
METRIC_LOG_CSV           = os.path.join(LOG_DIR, "metrics.csv")

STATE_DIR                = os.path.join(ROOT, "state")
BANDIT_PATH              = os.path.join(STATE_DIR, "bandit.json")
USED_SET_PATH            = os.path.join(STATE_DIR, "used_set.json")

MAX_TWEET_LEN            = 280
PRIMARY_MAX              = 190   # opener (no link)
REPLY_MAX                = 265   # reply with link + hashtags
HASHTAGS_MAX             = 2

ASIN_RE                  = re.compile(r"\b[A-Z0-9]{10}\b")
random.seed()

# ---------- SETUP ----------
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)
if not os.path.exists(TWEET_LOG_CSV):
    with open(TWEET_LOG_CSV, "w", newline="", encoding="utf-8") as f:
        csv.writer(f)..writerow(["ts","mode","product_title","asin","tweet_id_1","tweet_id_2","link","status"])
if not os.path.exists(METRIC_LOG_CSV):
    with open(METRIC_LOG_CSV, "w", newline="", encoding="utf-8") as f:
        csv.writer(f)..writerow(["ts","tweet_id","likes","replies","retweets","quotes"])

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# v2 for tweets / v1.1 for media upload
x_client_v2 = tweepy.Client(
    consumer_key=X_API_KEY,
    consumer_secret=X_API_SECRET,
    access_token=X_ACCESS_TOKEN,
    access_token_secret=X_ACCESS_SECRET
)
auth_v1 = tweepy.OAuth1UserHandler(X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET)
x_api_v1 = tweepy.API(auth_v1)  # for media upload

# ---------- DATA ----------
@dataclass
class Product:
    title: str
    asin: Optional[str]
    category: Optional[str]
    keywords: List[str]
    image_path: Optional[str]
    benefits: List[str]
    price_anchor: Optional[str]

def parse_products(path: str) -> List[Product]:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            asin = (r.get("asin") or "").strip().upper() or None
            if asin and not ASIN_RE.match(asin):
                asin = None
            kws = [k.strip() for k in (r.get("keywords") or "").split("|") if k.strip()]
            bens = [b.strip() for b in (r.get("benefits") or "").split("|") if b.strip()]
            img = (r.get("image_path") or "").strip() or None
            out.append(Product(
                title=(r.get("title") or "").strip(),
                asin=asin,
                category=(r.get("category") or "").strip() or None,
                keywords=kws,
                image_path=os.path.join(IMAGES_DIR, img) if img else None,
                benefits=bens,
                price_anchor=(r.get("price_anchor") or "").strip() or None
            ))
    return out

def load_json(path, default):
    try:
        with open(path,"r",encoding="utf-8") as f: return json.load(f)
    except Exception:
        return default

def save_json(path, obj):
    with open(path,"w",encoding="utf-8") as f: json.dump(obj,f,indent=2)

def normalize(s:str) -> str:
    return re.sub(r"\s+"," ",s.strip().lower())

# ---------- BANDIT ----------
DEFAULT_MODES = ["spiky","confession","problem_fix","brand_tax","micro_drill","two_choice"]
def load_bandit():
    b = load_json(BANDIT_PATH, {})
    if not b:
        b = {m: {"w":1.0, "n":0, "r":0.0} for m in DEFAULT_MODES}
        save_json(BANDIT_PATH, b)
    return b

def choose_mode(bandit, eps=0.25):
    if random.random() < eps:
        return random.choice(DEFAULT_MODES)
    # exploit
    return max(bandit.items(), key=lambda kv: kv[1]["w"])[0]

def update_bandit(bandit, mode, reward):
    st = bandit.get(mode, {"w":1.0,"n":0,"r":0.0})
    st["n"] += 1
    st["r"] += reward
    st["w"] = max(0.2, st["r"] / st["n"])  # mean reward, floor to keep exploration alive
    bandit[mode] = st
    save_json(BANDIT_PATH, bandit)

# ---------- LINKS ----------
def build_aff_link(product: Product, mode: str) -> str:
    tag = TRACKING_IDS_BY_MODE.get(mode, AFFILIATE_TAG)
    if product.asin:
        return f"https://www.amazon.com/dp/{product.asin}/?tag={tag}"
    # Fallback to search
    q = urllib.parse.quote_plus(product.title or " ".join(product.keywords))
    return f"https://www.amazon.com/s?k={q}&tag={tag}"

# ---------- PROMPTS ----------
MODE_TEMPLATES = {
"spiky": """
You are a brutally honest shopper with strong opinions. Write TWO JSON blocks:
1) "primary": a spiky but defensible take (no link, no hashtags, no emojis) about the product below (<= {primary_max} chars). Do NOT sound like an ad. No brand superlatives.
2) "reply": a follow-up that states 1-2 concrete benefits (short phrases), then a very short CTA like "details + today’s price:" (<= {reply_max} chars without link).
Avoid clichés like "game-changer", "must-have". Be specific, tactile.

Return:
{{"primary":"...", "reply":"...", "hashtags":["tag1","tag2"]}}

Product: {title}
Category: {category}
Benefits: {benefits}
Price anchor (optional context): {price_anchor}
""",
"confession": """
Voice: candid confession after months of use. Same JSON schema as spiky. Keep it grounded, specific, slightly self-deprecating. No hashtags in primary.
Constraints: no emojis, no hype adjectives, <= {primary_max} chars primary, <= {reply_max} chars reply.
Product: {title} | Benefits: {benefits}
""",
"problem_fix": """
Voice: concise problem -> one-move fix. Same JSON schema. Primary states the problem crisply; reply states the fix with 1-2 benefits + short CTA.
No emojis. No hashtags in primary. Length limits as above.
Product: {title} | Benefits: {benefits}
""",
"brand_tax": """
Voice: anti-brand-tax. Primary contrasts "logo price" vs utility. Reply gives concrete benefit + CTA. Avoid naming specific competitor brands.
Schema + limits identical. Product: {title} | Benefits: {benefits}
""",
"micro_drill": """
Voice: nerdy micro-detail only real users notice. Primary = tiny insight, oddly satisfying. Reply = 1-2 benefits + CTA. Schema + limits identical.
Product: {title} | Benefits: {benefits}
""",
"two_choice": """
Voice: fork-in-the-road. Primary frames A vs B (behavioral choice). Reply: recommend this product for one branch + CTA. Schema + limits identical.
Product: {title} | Benefits: {benefits}
"""
}

def ai_generate(mode:str, product: Product) -> Tuple[str,str,List[str]]:
    tpl = MODE_TEMPLATES[mode]
    prompt = tpl.format(
        title=product.title, category=product.category or "general",
        benefits=", ".join(product.benefits) if product.benefits else "n/a",
        price_anchor=product.price_anchor or "n/a",
        primary_max=PRIMARY_MAX, reply_max=REPLY_MAX
    )
    resp = openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role":"user","content": prompt}],
        temperature=0.9 if mode in ("spiky","brand_tax") else 0.7,
        top_p=0.95,
        presence_penalty=0.7,
        frequency_penalty=0.2,
        max_tokens=400
    )
    raw = resp.choices[0].message.content.strip()
    # harden JSON parsing
    try:
        j = json.loads(raw)
        primary = j["primary"].strip()
        reply   = j["reply"].strip()
        tags    = [t.strip().lstrip("#") for t in j.get("hashtags", []) if t.strip()][:HASHTAGS_MAX]
    except Exception as e:
        raise RuntimeError(f"LLM JSON parse failed: {e} | RAW: {raw[:220]}")
    if len(primary) > PRIMARY_MAX: primary = primary[:PRIMARY_MAX-1] + "…"
    if len(reply) > REPLY_MAX: reply = reply[:REPLY_MAX-1] + "…"
    return primary, reply, tags

# ---------- POSTING ----------
def upload_media_if_any(path:str) -> Optional[int]:
    if not path or not os.path.exists(path): return None
    media = x_api_v1.media_upload(filename=path)
    return media.media_id

def post_thread(primary:str, reply:str, link:str, hashtags:List[str], image_path:Optional[str]) -> Tuple[str, Optional[str]]:
    # T1: no link, no hashtags
    t1 = x_client_v2.create_tweet(text=primary)
    t1_id = t1.data["id"]

    # T2: reply with link + minimal hashtags
    hline = " ".join(f"#{h}" for h in hashtags[:HASHTAGS_MAX])
    body = f"{reply}\n{link}\n\n{hline}".strip()
    if len(body) > MAX_TWEET_LEN:
        body = body[:MAX_TWEET_LEN-1] + "…"

    media_id = upload_media_if_any(image_path)
    if media_id:
        t2 = x_client_v2.create_tweet(text=body, in_reply_to_tweet_id=t1_id, media_ids=[media_id])
    else:
        t2 = x_client_v2.create_tweet(text=body, in_reply_to_tweet_id=t1_id)
    return t1_id, t2.data["id"]

def log_tweet(mode, product:Product, t1_id, t2_id, link, status):
    with open(TWEET_LOG_CSV, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            mode, product.title, product.asin or "",
            t1_id or "", t2_id or "", link, status
        ])

# ---------- USED-SET ----------
def load_used_set() -> set:
    s = set(load_json(USED_SET_PATH, []))
    return s

def save_used_set(s:set):
    save_json(USED_SET_PATH, sorted(list(s)))

def choose_product(products: List[Product]) -> Product:
    used = load_used_set()
    avail = [p for p in products if normalize(p.title) not in used]
    if not avail:
        # allow repeats, but prefer those with ASIN first
        avail = sorted(products, key=lambda p: (p.asin is None, normalize(p.title)))
        used.clear()
    choice = random.choice(avail)
    used.add(normalize(choice.title))
    save_used_set(used)
    return choice

# ---------- MAIN ----------
def main():
    products = parse_products(PRODUCT_CSV)
    if not products:
        raise RuntimeError("No products loaded. Provide products.csv with headers: title,asin,category,keywords,image_path,benefits,price_anchor")

    bandit = load_bandit()
    mode = choose_mode(bandit, eps=0.25)
    product = choose_product(products)
    link = build_aff_link(product, mode)

    try:
        primary, reply, tags = ai_generate(mode, product)
        t1, t2 = post_thread(primary, reply, link, tags, product.image_path)
        log_tweet(mode, product, t1, t2, link, "success")
        notify_slack("ProductBot", "success", f"Mode={mode}\n{product.title}\nT1={t1}\nT2={t2}")
        print("[✓] Posted thread.", t1, t2)
    except Exception as e:
        log_tweet(mode, product, "", "", link, f"fail:{e}")
        notify_slack("ProductBot", "fail", f"{type(e).__name__}: {e}")
        raise

if __name__ == "__main__":
    main()
