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

def fetch_reddit_trends():
    reddit = reddit_client()
    now = time.time()
    max_age = 60 * 60 * 24  # 24 hours
    trends = []
    titles_seen = set()

    subreddits = ["all", "TrueOffMyChest", "antiwork", "confession", "AmItheAsshole"]

    def extract_from(sub, posts):
        for post in posts:
            if post.stickied or post.over_18:
                continue
            if now - post.created_utc > max_age:
                continue
            title = post.title.strip()
            if title in titles_seen:
                continue
            if len(title) > 15 and not title.lower().startswith(("til", "meirl", "oc", "ama")):
                trends.append({
                    "title": title,
                    "subreddit": post.subreddit.display_name,
                    "score": post.score,
                    "created_utc": post.created_utc
                })
                titles_seen.add(title)

    try:
        for sub in subreddits:
            extract_from(sub, reddit.subreddit(sub).hot(limit=30))
        return trends
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
    f"You are a hyper‑reactive, wildly unpredictable Twitter addict who blurts out spicy takes for sport. "
    f"A current social trend simmers in the background of your mind.\n\n"
    f"SUBTEXT (never quote directly):\n"
    f"• Trend/Event: \"{{trend_title}}\"\n"
    f"• Reddit Vibe Cues: {{context}}\n\n"
    f"Write ONE standalone tweet ≤ 150 characters—chaotic, smug, petty, or dark‑funny—peppered with side‑eye or analogies that clearly *echo* the trend’s energy without naming it outright. "
    f"No hashtags, links, or emojis in the main tweet.\n\n"
    f"Then add:\n"
    f"• ONE brief reply/CTA ≤ 40 chars (sarcastic, delusional, defensive—your pick).\n"
    f"• ONE *real, currently trending* hashtag *including the # symbol* (separate field).\n\n"
    f"Return **only** valid JSON with keys:\n"
    f"{{\n"
    f"  \"tweet\": str,   # ≤150 chars, no emojis, no hashtags\n"
    f"  \"cta\": str,     # ≤40 chars, no emojis, no hashtags\n"
    f"  \"hashtag\": str  # starts with #, real & trending\n"
    f"}}\n\n"
    f"Total tweet+cta ≤ 220 chars. "
    f"If you reference the trend directly, regenerate. "
    f"Stay viral, unpredictable, and never melancholy."
)

    try:
        res = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.95,
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
