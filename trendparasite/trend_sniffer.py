import os
import json
import datetime
import random
from pathlib import Path
import praw
import tweepy
from openai import OpenAI
from dotenv import load_dotenv

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
# GPT-4 Tweet Generator
# ─────────────────────────────────────
def generate_tweet(trend_title):
    context = fetch_reddit_context(trend_title)
    prompt = f"""
You are a chaotic, emotionally unstable Twitter poster who spirals online for attention. You post like you're always one unpaid bill away from snapping.

You're not reacting to news or trends — you're just *posting*.

Trend Concept (only for subtext): "{trend_title}"

Reddit Commentary (for tonal inspiration, not quoting):
{context}

Your task:
- Write one VERY SHORT standalone tweet that feels like it came from a real human who’s tired, impulsive, bitter, or unhinged.
- DO NOT say or imply you're reacting to a trend. No references to “this story,” “Reddit,” or “news.”
- DO NOT use emojis or hashtags in the tweet itself.
- Write like you’re posting at 2AM while rage-scrolling and eating string cheese.
- Be bold, fragmented, petty, or weird. Grammar mistakes are allowed.
- You can spiral mid-sentence or end abruptly.
- Then include a short CTA or fake reply (≤40 characters). This should contrast or collapse the tone of the main tweet — think regret, pettiness, or spiral.
- Finally, give exactly one **real, relevant hashtag** that could help this post go viral.
    - It must be a real hashtag used in current online culture — either topical (#Inflation, #Election2024) or emotional (#OkSure, #ThisIsFine).
    - Do NOT invent hashtags. No formatting like #MyRandomThought or #LateNightMood.
    - Do NOT use generic fluff like #ExistentialCrisis, #Thoughts, or #Life.
    - Choose a hashtag people might *actually search* or browse.

Return a JSON object with:
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
        return res.choices[0].message.content.strip()
    except Exception as e:
        return f"ERROR: {e}"

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


# ─────────────────────────────────────
# MAIN
# ─────────────────────────────────────
if __name__ == "__main__":
    print(f"🗓️ TrendParasite — {datetime.datetime.now().strftime('%Y-%m-%d')}")

    # Load trends and filter out used ones
    trends = fetch_reddit_trends()
    memory = load_memory()
    recent = {entry["trend"] for entry in memory}
    fresh_trends = [t for t in trends if t not in recent]

    if not fresh_trends:
        print("🛑 No fresh trends available.")
        exit()

    # Rank and select best
    ranked = score_trends(fresh_trends)
    selected = ranked[0]
    save_trend_to_memory(selected)

    # Generate tweet
    print(f"\n🧠 Selected Trend: {selected}\n")
    output_raw = generate_tweet(selected)

    try:
        output = json.loads(output_raw)
        tweet = output.get("tweet", "").strip()
        cta = output.get("cta", "").strip()
        hashtag = output.get("hashtag", "").strip()

        if not tweet or not cta or not hashtag:
            raise ValueError("Missing one or more required keys.")

        # Combine into single tweet
        full_tweet = f"{tweet}\n{cta} {hashtag}"
        print("📤 Final Output:")
        print(json.dumps({"tweet": full_tweet}, indent=2))

        # Post the full tweet
        post_to_twitter(full_tweet)

    except Exception as e:
        print("❌ Error parsing tweet output:", e)
        print("🔎 Raw output from GPT:")
        print(output_raw)
