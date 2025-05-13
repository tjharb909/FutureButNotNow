import json
import datetime
from pathlib import Path
import praw
import tweepy
from openai import OpenAI
from dotenv import load_dotenv
import sys
import os
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
VIRAL_KEYWORDS = ["dies", "ban", "leak", "update", "fired", "explodes", "AI", "GPT", "parody", "war", "meme", "love"]

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

def fetch_reddit_trends():
    reddit = reddit_client()
    titles = set()
    try:
        for post in reddit.subreddit("all").hot(limit=50):
            if post.stickied or post.over_18:
                continue
            title = post.title.strip()
            if len(title) > 15 and not title.lower().startswith(("til", "meirl", "oc", "ama")):
                titles.add(title)
        return sorted(titles, key=lambda x: -len(x))[:15]
    except Exception as e:
        print("❌ Reddit API error:", e)
        return []

def fetch_reddit_context(trend):
    reddit = reddit_client()
    try:
        search_results = reddit.subreddit("all").search(trend, sort="relevance", limit=5)
        context_lines = []
        for post in search_results:
            if len(post.title) > 20:
                context_lines.append(f"- {post.title.strip()}")
            if post.selftext:
                snippet = post.selftext.strip().splitlines()[0]
                if snippet and len(snippet) > 30:
                    context_lines.append(f"  {snippet[:200].strip()}")
            post.comments.replace_more(limit=0)
            for c in post.comments[:3]:
                body = c.body.strip()
                if body and len(body) > 30 and "http" not in body.lower():
                    context_lines.append(f"  {body[:200].strip()}")
        return "\n".join(context_lines[:8]) or "(No relevant Reddit context found.)"
    except Exception as e:
        return f"(Failed to fetch Reddit context: {e})"

# ─────────────────────────────────────
# Memory
# ─────────────────────────────────────
def load_memory():
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text())
    return []

def save_trend_to_memory(trend):
    memory = load_memory()
    memory.append({"trend": trend, "date": str(datetime.date.today())})
    MEMORY_FILE.write_text(json.dumps(memory[-100:], indent=2))

# ─────────────────────────────────────
# Trend Scoring
# ─────────────────────────────────────
def score_trends(trends):
    weights = []
    for t in trends:
        score = 0
        t_lower = t.lower()
        if any(k in t_lower for k in VIRAL_KEYWORDS):
            score += 10
        if "?" in t:
            score += 2
        if len(t) > 100:
            score -= 5
        score += sum(1 for word in t_lower.split() if word.istitle())
        weights.append((score, t))
    weights.sort(reverse=True)
    return [t for score, t in weights]

# ─────────────────────────────────────
# Gigaom — GPT-4 Tweet Generator
# ─────────────────────────────────────
def generate_tweet(trend_title):
    context = fetch_reddit_context(trend_title)
    prompt = f"""
You are an emotionally unstable, impulsive Twitter user who spirals in public. You post like it's always 2AM and you're holding it together with sarcasm, snacks, and spite.

Trend Context (for background tone only): "{trend_title}"
Reddit Commentary (vibe cues only, do NOT quote):
{context}

Your task:
- Write ONE short, standalone tweet.
- Tone: chaotic, tired, bitter, impulsive, or deeply unwell — but self-aware.
- It should sound like a real human trying to cope, spiral, or joke their way through something unraveling.
- You're not reacting to any trend, event, or news. You’re just posting into the void.
- Be emotionally charged, messy, abrupt, or contradictory.
- Grammar mistakes or fractured logic are OK.
- **NO** emojis.
- **NO** hashtags in the tweet itself.

Then:
- Include ONE short fake reply or CTA (max 40 characters). It should contrast the main tweet’s tone — collapse, regret, sarcasm, or numbness.
- Add ONE **real, currently-viral hashtag** people might actually click or search (e.g. topical, ironic, or emotional — but not made-up or generic).
    - It must be a real, active hashtag.
    - Do NOT use vague fluff like #Life or #Thoughts.
    - Do NOT invent hashtags.

Return exactly this JSON:
{{ 
  "tweet": "...", 
  "cta": "...", 
  "hashtag": "#RealTrendingHashtag" 
}}
"""
    try:
        res = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.95
        )
        return res.choices[0].message.content.strip(), context
    except Exception as e:
        return f"ERROR: {e}", None

# ─────────────────────────────────────
# Twitter Posting (Main Tweet Only)
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
    memory = load_memory()
    recent = {entry["trend"] for entry in memory}
    fresh_trends = [t for t in trends if t not in recent]

    if not fresh_trends:
        print("🛑 No fresh trends available.")
        exit()

    ranked = score_trends(fresh_trends)
    selected = ranked[0]
    save_trend_to_memory(selected)

    print(f"🧠 Selected Trend: {selected}")
    output_raw, context = generate_tweet(selected)

    try:
        output = json.loads(output_raw)
        tweet = output.get("tweet", "").strip()
        cta = output.get("cta", "").strip()
        hashtag = output.get("hashtag", "").strip()
        if not tweet or not cta or not hashtag:
            raise ValueError("Missing required tweet components.")
        full_tweet = f"{tweet}\n{cta} {hashtag}"
        print("📤 Final Output:")
        print(json.dumps({"tweet": full_tweet}, indent=2))
        post_to_twitter(full_tweet)
        notify_slack(
            bot_name="TrendParasite",
            status="success",
            message_block=f"Tweet posted successfully.",
            trend=selected,
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
            trend=selected,
            tweet=output_raw if isinstance(output_raw, str) else str(output_raw),
            hashtag="(unknown)",
            context=context or "(no context)"
        )
