import os
import requests
import openai
import time
import tweepy
import random
import os
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "utils"))

from slack_notifier import notify_slack

# CONFIG
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TEST_MODE = False  # Set to False when you're ready to post

# Twitter API setup
TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")
TWITTER_API_SECRET = os.getenv("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_SECRET = os.getenv("TWITTER_ACCESS_SECRET")

# === SETUP ===
client = openai.OpenAI(api_key=OPENAI_API_KEY)

twitter_client = tweepy.Client(
    consumer_key=TWITTER_API_KEY,
    consumer_secret=TWITTER_API_SECRET,
    access_token=TWITTER_ACCESS_TOKEN,
    access_token_secret=TWITTER_ACCESS_SECRET
)

# === FUNCTIONS ===

def fetch_news():
    url = f"https://newsdata.io/api/1/news?apikey={NEWS_API_KEY}&language=en&country=us&category=top"
    r = requests.get(url)
    articles = r.json().get("results", [])
    return articles[:1]

def build_prompt(title, description, context, tone):
    label = "left-leaning progressive" if tone == "left" else "right-leaning conservative"
    return f"""
You are a viral and contrversial Twitter bot. You post a single short viral and controversial tweet reacting to real news. The tweet must sound like a {label} and fit in a single post (≤250 characters).

News Title: {title}
Summary: {description}
Details: {context}

Write a bold tweet with only one applicable and trending hashtag, NO links, NO emojis. Prefix the tweet with:
🟦 [Left] — if tone is left
🟥 [Right] — if tone is right

Avoid politeness. Be blunt and viral.
"""

def generate_single_tweet(article):
    title = article.get("title", "")
    description = article.get("description", "")
    content = article.get("content", description)
    tone = random.choice(["left", "right"])
    label = "[🟦 Left]" if tone == "left" else "[🟥 Right]"

    prompt = build_prompt(title, description, content, tone)
    res = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        temperature=1,
        max_tokens=300
    )

    tweet = res.choices[0].message.content.strip()
    return tweet[:280]  # Truncate in case

def post_to_twitter(text):
    try:
        twitter.update_status(status=text)
        print("✅ Tweet posted.")
    except Exception as e:
        print("❌ Error posting tweet:", e)
        notify_slack("Right/Left Bot", "fail", f"Error:\n{str(outer)}")

def run_bot():
    print("📰 Fetching news...")
    articles = fetch_news()
    if not articles:
        print("⚠️ No articles found.")
        notify_slack("Right/Left Bot", "fail", "OpenAI generation failed.")
        return

    article = articles[0]
    print(f"\n🔗 Topic: {article['title']}")
    tweet = generate_single_tweet(article)

    print("\n🧪 Generated Tweet:\n", tweet)
    if not TEST_MODE:
        twitter_client.create_tweet(text=tweet)
        print("✅ Tweet posted.")
        notify_slack("Right/Left Bot", "success", f"Posted:\n{tweet}")

# === RUN ===
if __name__ == "__main__":
    run_bot()
