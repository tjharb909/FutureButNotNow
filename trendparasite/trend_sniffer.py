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
import re
from textblob import TextBlob
from collections import Counter
import requests
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
    """Fetch and analyze Reddit context with relevance scoring"""
    reddit = reddit_client()
    
    try:
        posts = list(reddit.subreddit("all").search(trend, sort="relevance", limit=15))
        if not posts:
            return "No relevant Reddit context found."
        
        # Score posts by relevance
        scored_posts = []
        for post in posts:
            relevance = calculate_relevance(post.title, trend)
            if relevance > 0.3:  # Filter low relevance
                scored_posts.append((post, relevance))
        
        scored_posts.sort(key=lambda x: x[1], reverse=True)
        top_posts = [post for post, _ in scored_posts[:5]]
        
        # Build enhanced context
        context_parts = [
            f"SUMMARY: {summarize_posts(top_posts)}",
            f"SENTIMENT: {analyze_sentiment(top_posts)}",
            f"KEYWORDS: {', '.join(extract_keywords(top_posts))}",
            f"ENGAGEMENT: {get_engagement_signals(top_posts)}"
        ]
        
        return "\n".join(context_parts)
        
    except Exception as e:
        return f"Context fetch failed: {e}"

def calculate_relevance(post_title: str, trend: str) -> float:
    """Calculate relevance score between post and trend"""
    trend_words = set(trend.lower().split())
    post_words = set(post_title.lower().split())
    
    # Jaccard similarity + title overlap bonus
    intersection = len(trend_words.intersection(post_words))
    union = len(trend_words.union(post_words))
    jaccard = intersection / union if union > 0 else 0
    
    # Bonus for exact phrase matches
    if trend.lower() in post_title.lower():
        jaccard += 0.3
    
    return min(jaccard, 1.0)

def summarize_posts(posts) -> str:
    """Create concise summary of top posts"""
    if not posts:
        return "No relevant posts found."
    
    summaries = []
    for post in posts[:3]:
        # Extract key info
        title = post.title[:100]
        score = post.score
        comments = post.num_comments
        
        # Get top comment if available
        top_comment = ""
        try:
            post.comments.replace_more(limit=0)
            if post.comments:
                top_comment = post.comments[0].body[:150]
        except:
            pass
        
        summary = f"• {title} ({score}↑, {comments} comments)"
        if top_comment and len(top_comment) > 20:
            summary += f"\n  Top comment: {top_comment}..."
        
        summaries.append(summary)
    
    return "\n".join(summaries)

def analyze_sentiment(posts) -> str:
    """Analyze overall sentiment of discussions"""
    all_text = []
    
    for post in posts:
        all_text.append(post.title)
        if hasattr(post, 'selftext') and post.selftext:
            all_text.append(post.selftext[:500])
        
        # Sample comments
        try:
            post.comments.replace_more(limit=0)
            for comment in post.comments[:5]:
                if hasattr(comment, 'body'):
                    all_text.append(comment.body[:200])
        except:
            pass
    
    combined_text = " ".join(all_text)
    blob = TextBlob(combined_text)
    
    polarity = blob.sentiment.polarity
    if polarity > 0.1:
        return "positive"
    elif polarity < -0.1:
        return "negative"
    else:
        return "neutral"

def extract_keywords(posts) -> list:
    """Extract trending keywords from discussions"""
    all_words = []
    
    for post in posts:
        words = re.findall(r'\b[a-zA-Z]{3,}\b', post.title.lower())
        all_words.extend(words)
    
    # Filter common words and get top keywords
    common_words = {'the', 'and', 'for', 'are', 'but', 'not', 'you', 'all', 'can', 'this', 'that', 'with', 'have', 'was', 'will', 'they', 'been', 'said', 'what', 'when', 'how', 'why', 'who', 'where'}
    filtered_words = [w for w in all_words if w not in common_words and len(w) > 3]
    
    return [word for word, count in Counter(filtered_words).most_common(5)]

def get_engagement_signals(posts) -> dict:
    """Analyze engagement patterns"""
    if not posts:
        return {"avg_score": 0, "avg_comments": 0, "controversy": "low"}
    
    scores = [p.score for p in posts]
    comments = [p.num_comments for p in posts]
    
    avg_score = sum(scores) / len(scores)
    avg_comments = sum(comments) / len(comments)
    
    # Controversy indicator (high comments relative to upvotes)
    controversy_ratio = avg_comments / max(avg_score, 1)
    controversy = "high" if controversy_ratio > 0.5 else "medium" if controversy_ratio > 0.2 else "low"
    
    return {
        "avg_score": int(avg_score),
        "avg_comments": int(avg_comments),
        "controversy": controversy
    }

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
    
    prompt = f"""Create viral Twitter content for this trending topic.

Topic: "{trend_title}"
Context: {context[:600]}

Requirements:
- 200-250 characters total
- Natural trend reference (not forced exact wording)
- Genuine insight, hot take, or relatable angle
- Match discussion sentiment and energy

Good examples:
- "Everyone's arguing about X but the real issue is Y affecting millions daily"
- "X trending while I'm still confused about last week's Y drama"
- "Plot twist: X isn't about Y, it's actually Z and here's why..."

Bad examples:
- "X is trending, thoughts?" (too generic)
- "Can't believe X happened!" (pure reaction)

Return JSON only:
{{
  "tweet": "main content with trend reference",
  "cta": "call-to-action under 25 chars", 
  "hashtag": "#TrendingTag"
}}

Limits: tweet ≤200, cta ≤25, total ≤250. Be substantive, not reactive."""

    try:
        res = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
            top_p=0.9,
            frequency_penalty=0.3,
            presence_penalty=0.2,
            max_tokens=250
        )
        return res.choices[0].message.content.strip(), context
    except Exception as e:
        return f"ERROR: {e}", context

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
