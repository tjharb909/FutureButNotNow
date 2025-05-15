import json
import datetime
import time
from pathlib import Path
import praw
import tweepy
from openai import OpenAI
from dotenv import load_dotenv
import sys
import os
import random, functools, difflib
from collections import OrderedDict
from typing import List, Dict

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "utils"))
from slack_notifier import notify_slack

# Load environment variables from .env if exists
load_dotenv()

# Initialize OpenAI client
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# ─────────────────────────────────────
# Constants & Files
# ─────────────────────────────────────
MEMORY_FILE = Path("used_trends.json")
TREND_METADATA_FILE = Path("trend_metadata.json")

VIRAL_KEYWORDS = [
    "dies", "ban", "leak", "update", "fired", "explodes",
    "AI", "GPT", "parody", "war", "meme", "love"
]

# ─────────────────────────────────────
# Reddit Client (via PRAW)
# ─────────────────────────────────────
def reddit_client():
    return praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        username=os.environ["REDDIT_USERNAME"],
        password=os.environ["REDDIT_PASSWORD"],
        user_agent=os.environ["REDDIT_USER_AGENT"]
    )


# ──── Helpers ───────────────────────────────────────────────────────────────────

_H1  = 60 * 60
_MAX_AGE   = 24 * _H1          # candidate posts ≤ 24 h old
_MAX_STORE = 200               # remember up to 200 used titles
_HISTORY   = os.getenv("TREND_HISTORY_FILE", ".cache/used_trends.json")

# ──────────────────────────────────────────────────────────────────────────
def _load_history() -> set[str]:
    try:
        with open(_HISTORY, "r", encoding="utf8") as f:
            data = json.load(f)
            return {d["title"] for d in data if time.time() - d["ts"] < _MAX_AGE}
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def _save_history(add_title: str) -> None:
    os.makedirs(os.path.dirname(_HISTORY), exist_ok=True)
    try:
        with open(_HISTORY, "r+", encoding="utf8") as f:
            data = json.load(f)
    except Exception:
        data = []
    data.append({"title": add_title, "ts": time.time()})
    data = data[-_MAX_STORE:]                # keep most recent
    with open(_HISTORY, "w", encoding="utf8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)

def _norm(t):  # simple fuzzy-dup helper
    return "".join(c for c in t.lower() if c.isalnum() or c.isspace())

def fetch_reddit_trends() -> List[Dict]:
    reddit = reddit_client()
    now    = time.time()
    seen   = _load_history()
    titles_norm, candidates = [], OrderedDict()

    subs = ["all", "TrueOffMyChest", "antiwork", "confession", "AmItheAsshole"]
    random.shuffle(subs)

    def maybe_add(post):
        if post.stickied or post.over_18: return
        if now - post.created_utc > _MAX_AGE: return
        title = post.title.strip()
        if len(title) <= 15 or title.lower().startswith(("til", "meirl", "oc","ama")):
            return
        if title in seen: return                          # already tweeted this day
        n = _norm(title)
        if any(difflib.SequenceMatcher(None, n, x).ratio() > .9 for x in titles_norm):
            return
        score = post.score / ((now - post.created_utc)/_H1 + 1)**1.3
        candidates[title] = {
            "title": title,
            "subreddit": post.subreddit.display_name,
            "score": post.score,
            "created_utc": post.created_utc,
            "trend_score": score,
        }
        titles_norm.append(n)

    for sub in subs:
        for p in reddit.subreddit(sub).hot(limit=40):
            maybe_add(p)
        time.sleep(0.4)

    picked = sorted(candidates.values(), key=lambda d: d["trend_score"], reverse=True)
    if not picked:                                            # fallback to anything
        picked = [{"title": t} for t in seen][-1:]

    choice = random.choice(picked[:10])                       # variety!
    _save_history(choice["title"])
    return [choice]                                           # keep existing shape

# ──────────────────────────────────────────────────────────────────────────
def fetch_reddit_context(trend: str) -> str:
    reddit = reddit_client()
    try:
        rs = reddit.subreddit("all").search(trend, sort="relevance", limit=6)
        lines = []
        for p in rs:
            flair = f"[{p.link_flair_text}] " if p.link_flair_text else ""
            if len(p.title) > 20:
                lines.append(f"- {flair}{p.title.strip()}")
            if p.selftext:
                s = p.selftext.strip().splitlines()[0][:200]
                if len(s) > 30:
                    lines.append(f"  {s}")
            p.comments.replace_more(limit=0)
            for c in sorted(p.comments, key=lambda x: getattr(x,"score",0), reverse=True)[:3]:
                body = c.body.strip()
                if len(body) > 30 and "http" not in body.lower():
                    lines.append(f"  ({c.score}↑) {body[:200]}")
        return "\n".join(lines[:8]) or "(No relevant Reddit context found.)"
    except Exception as e:
        return f"(Failed to fetch Reddit context: {e})"

# ─────────────────────────────────────
# Memory (24-Hour Reset)
# ─────────────────────────────────────
def load_memory():
    if not MEMORY_FILE.exists():
        return []
    memory = json.loads(MEMORY_FILE.read_text())
    today = str(datetime.date.today())
    return [entry for entry in memory if entry.get("date") == today]

def save_trend_to_memory(trend):
    today = str(datetime.date.today())
    memory = [entry for entry in load_memory() if entry.get("date") == today]
    memory.append({"trend": trend, "date": today})
    MEMORY_FILE.write_text(json.dumps(memory[-100:], indent=2))

def save_trend_metadata(trend_obj):
    data = []
    if TREND_METADATA_FILE.exists():
        data = json.loads(TREND_METADATA_FILE.read_text())
    trend_obj["date"] = str(datetime.date.today())
    data.append(trend_obj)
    TREND_METADATA_FILE.write_text(json.dumps(data[-200:], indent=2))

# ─────────────────────────────────────
# Trend Scoring
# ─────────────────────────────────────
def score_trends(trends):
    weights = []
    now = time.time()

    for trend in trends:
        title = trend["title"]
        t_lower = title.lower()
        score = 0

        if any(k in t_lower for k in VIRAL_KEYWORDS):
            score += 10
        if "?" in title:
            score += 2
        if len(title) > 100:
            score -= 5
        score += sum(1 for word in t_lower.split() if word.istitle())

        if trend.get("score", 0) > 5000:
            score += 3
        age_minutes = (now - trend.get("created_utc", now)) / 60
        if age_minutes < 120:
            score += 2

        weights.append((score, trend))

    # ✅ Sort by score only (avoids comparing dicts)
    weights.sort(key=lambda x: x[0], reverse=True)
    return [t for score, t in weights]


# ─────────────────────────────────────
# GPT-4 Tweet Generator
# ─────────────────────────────────────
def generate_tweet(trend_title):
    context = fetch_reddit_context(trend_title)
    prompt = (
    f"You’re a volatile Twitter gremlin who gorges on trending chaos and spits out hotter takes. "
    f"Profanity tolerated, absurdity encouraged, no corporate tone.\n\n"
    f"Trend/Event (must be **named once**): \"{trend_title}\"\n"
    f"Reddit Vibe Cues: {context}\n\n"
    f"Write ONE standalone tweet (≤ 150 chars) that *directly nods* to the trend—roast it, brag about it, or fling a wild analogy. "
    f"Embrace contradictions, sentence fragments, caps, or side-quests. "
    f"No emojis or links inside the tweet.\n\n"
    f"Then add:\n"
    f"• ONE jagged CTA ≤ 40 chars (taunt, dare, mic-drop—your call).\n"
    f"• ONE currently-trending hashtag **with** the # sign as its own field.\n\n"
    f"Return ONLY this JSON ⇩\n"
    f"{{\n"
    f"  \"tweet\": str,\n"
    f"  \"cta\": str,\n"
    f"  \"hashtag\": str\n"
    f"}}\n\n"
    f"Limits: tweet ≤ 150, cta ≤ 40, combined ≤ 220. "
    f"Tweet must mention the trend once, contain no emojis or extra hashtags. "
    f"If you censor yourself, regenerate spicier."
)

    try:
        res = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            temperature=1.4,
            top_p=0.95,
            frequency_penalty=0.3,
            presence_penalty=0.6,
            max_tokens=250
        )
        return res.choices[0].message.content.strip(), context
    except Exception as e:
        return f"ERROR: {e}", None

# ─────────────────────────────────────
# Twitter Posting
# ─────────────────────────────────────
def post_to_twitter(full_tweet):
    try:
        client = tweepy.Client(
            consumer_key=os.environ["TWITTER_API_KEY"],
            consumer_secret=os.environ["TWITTER_API_SECRET"],
            access_token=os.environ["TWITTER_ACCESS_TOKEN"],
            access_token_secret=os.environ["TWITTER_ACCESS_SECRET"]
        )
        client.create_tweet(text=full_tweet)
        print("✅ Tweet posted successfully.")
    except Exception as e:
        print("❌ Twitter post failed:", e)
        raise

# ─────────────────────────────────────
# MAIN
# ─────────────────────────────────────
if __name__ == "__main__":
    print(f"🗓️ TrendParasite — {datetime.datetime.now().strftime('%Y-%m-%d')}")
    
    trends = fetch_reddit_trends()
    if not trends:
        print("🛑 Failed to fetch trends.")
        exit()

    memory = load_memory()
    recent_titles = {entry["trend"] for entry in memory}
    fresh_trends = [t for t in trends if t["title"] not in recent_titles]

    if not fresh_trends:
        print("🛑 No fresh trends available.")
        exit()

    ranked = score_trends(fresh_trends)
    selected = ranked[0]

    save_trend_to_memory(selected["title"])  # existing memory
    save_trend_metadata(selected)            # new metadata

    print(f"🧠 Selected Trend: {selected['title']}")
    output_raw, context = generate_tweet(selected["title"])

    try:
        output = json.loads(output_raw)
        tweet = output.get("tweet", "").strip()
        cta = output.get("cta", "").strip()
        hashtag = output.get("hashtag", "").strip()
        if not tweet or not cta or not hashtag:
            raise ValueError("Missing required tweet components.")
        full_tweet = f"{tweet}\n\n{cta} {hashtag}"
        print("📤 Final Output:")
        print(json.dumps({"tweet": full_tweet}, indent=2))
        post_to_twitter(full_tweet)
        notify_slack(
            bot_name="TrendParasite",
            status="success",
            message_block=f"Tweet posted successfully.",
            trend=selected["title"],
            tweet=full_tweet,
            hashtag=hashtag,
            context=context
        )
    except Exception as e:
        print("❌ Error parsing tweet:", e)
        print("🔎 Raw output:", output_raw)
        notify_slack(
            bot_name="TrendParasite",
            status="fail",
            message_block=f"Tweet failed.\n```{str(e)}```",
            trend=selected["title"],
            tweet=output_raw if isinstance(output_raw, str) else str(output_raw),
            hashtag="(unknown)",
            context=context or "(no context)"
        )
