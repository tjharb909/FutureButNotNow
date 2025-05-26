import random
import json
import urllib.parse
import tweepy
import re
import csv
from datetime import datetime
from openai import OpenAI
import os
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "utils"))

from slack_notifier import notify_slack

# === CONFIGURATION ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")
TWITTER_API_SECRET = os.getenv("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_SECRET = os.getenv("TWITTER_ACCESS_SECRET")

AFFILIATE_TAG = "futurebutnotn-20"
DEFAULT_AFFILIATE_LINK = "https://amzn.to/4jHNpOC"
MAX_TWEET_LENGTH = 280
MAX_BODY_LENGTH = 220

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCT_LIST_FILE = os.path.join(SCRIPT_DIR, "product_list.txt")
USED_PRODUCTS_FILE = os.path.join(SCRIPT_DIR, "used_products.txt")
LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "tweet_logs.csv")
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "utils"))

# === SETUP ===
openai_client = OpenAI(api_key=OPENAI_API_KEY)
twitter_client = tweepy.Client(
    consumer_key=TWITTER_API_KEY,
    consumer_secret=TWITTER_API_SECRET,
    access_token=TWITTER_ACCESS_TOKEN,
    access_token_secret=TWITTER_ACCESS_SECRET
)

# === FILE UTILITIES ===
def normalise(text: str) -> str:
    """Canonical form for deduplication (lower‑case, single spaces)."""
    return re.sub(r"\s+", " ", text.strip().lower())

def ensure_used_file():
    """Guarantee that used_products.txt exists before we read/append."""
    if not os.path.exists(USED_PRODUCTS_FILE):
        open(USED_PRODUCTS_FILE, "w", encoding="utf-8").close()

def ensure_log_folder():
    os.makedirs(LOG_DIR, exist_ok=True)
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline='', encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "product_title", "tweet_text",
                "cta", "hashtags", "affiliate_link", "status"
            ])

def log_tweet(product_title, tweet_text, cta, hashtags, link, status):
    with open(LOG_FILE, "a", newline='', encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.utcnow().isoformat(timespec="seconds"),
            product_title,
            tweet_text,
            cta,
            ", ".join(hashtags),
            link,
            status
        ])

# === PRODUCT LIST HANDLING ====================================

def get_curated_products():
    try:
        with open(PRODUCT_LIST_FILE, "r", encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]
    except Exception as e:
        print(f"[TXT PRODUCT ERROR]: {e}")
        return []

def get_used_products():
    ensure_used_file()
    with open(USED_PRODUCTS_FILE, "r", encoding="utf-8") as f:
        raw   = [ln.strip() for ln in f if ln.strip()]
        norms = {normalise(ln) for ln in raw}
    return raw, norms

def mark_product_as_used(product):
    with open(USED_PRODUCTS_FILE, "a", encoding="utf-8", newline="") as f:
        f.write(product.strip() + "\n")

def get_next_unused_product():
    curated       = get_curated_products()
    _raw, usedset = get_used_products()

    available = [p for p in curated if normalise(p) not in usedset]
    if not available:
        raise Exception("🛑 No unused products left. Please refill product_list.txt.")

    choice = random.choice(available)
    mark_product_as_used(choice)   # record immediately
    return choice

# === AI PROMPT ===
def create_prompt_from_product(product_title):
    return (
        f"You're writing as a genuine Twitter user who actually uses and enjoys products. "
        f"Write authentic, relatable content that sounds like real people talking about stuff they like.\n\n"
        f"Product: {product_title}\n\n"
        f"Create ONE tweet (≤ 150 chars) that:\n"
        f"• Mentions the product naturally (not in first 3 or last 5 words)\n"
        f"• Sounds like genuine user experience or opinion\n"
        f"• Uses casual, conversational tone - like texting a friend\n"
        f"• Can be: enthusiastic, mildly sarcastic, helpful, or relatable\n"
        f"• Avoid: obvious marketing speak, excessive hype, or cringe\n"
        f"• No emojis, hashtags, or links in the main tweet\n\n"
        f"Tone examples:\n"
        f"• 'honestly didn't expect [product] to be this good but here we are'\n"
        f"• 'why did nobody tell me about [product] sooner'\n"
        f"• 'me: I don't need [product] also me: *orders immediately*'\n"
        f"• '[product] is solving problems I didn't know I had'\n\n"
        f"Then add:\n"
        f"• One natural follow-up/CTA ≤ 40 chars\n"
        f"• Two relevant hashtags (camelCase, no # symbol)\n"
        f"• Amazon search keywords\n\n"
        f"Return ONLY this JSON:\n"
        f"{{\n"
        f"  \"tweet\": \"your main tweet text\",\n"
        f"  \"cta\": \"short follow-up\",\n"
        f"  \"hashtags\": [\"tag1\", \"tag2\"],\n"
        f"  \"keywords\": \"search terms for amazon\"\n"
        f"}}\n\n"
        f"Limits: tweet ≤ 150 chars, cta ≤ 40 chars, combined ≤ 220 chars total."
    )



def get_ai_tweet(product_title, retries=2):
    prompt = create_prompt_from_product(product_title)
    for attempt in range(retries):
        try:
            response = openai_client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                top_p=0.95,
                frequency_penalty=0.2,
                presence_penalty=0.6,
                max_tokens=250
            )
            return json.loads(response.choices[0].message.content.strip())
        except Exception as e:
            print(f"[OpenAI Attempt {attempt+1} ERROR]: {e}")
    return None

# === FORMATTING ===
def generate_affiliate_link(keywords, product_title=None):
    if product_title:
        if isinstance(product_title, list):
            product_title = " ".join(product_title)
        encoded_title = urllib.parse.quote_plus(str(product_title).strip())
        return f"https://www.amazon.com/s?k={encoded_title}&tag={AFFILIATE_TAG}"
    elif keywords:
        keyword_phrase = "+".join(urllib.parse.quote_plus(k) for k in keywords)
        return f"https://www.amazon.com/s?k={keyword_phrase}&tag={AFFILIATE_TAG}"
    else:
        return DEFAULT_AFFILIATE_LINK

def format_generated_tweet(tweet_text, cta, hashtags, link):
    body = f"{tweet_text} {cta}".strip()
    hashtag_line = " ".join(f"#{tag}" for tag in hashtags[:2])
    full = f"{body}\n {link}\n\n{hashtag_line}"
    if len(full) > MAX_TWEET_LENGTH:
        print("[Warning] Truncated tweet due to length")
        return full[:MAX_TWEET_LENGTH - 1] + "…"
    return full

# === MAIN ===
def post_to_twitter():
    ensure_log_folder()
    try:
        product_title = get_next_unused_product()
        ai_data = get_ai_tweet(product_title)
        if not ai_data or not all(k in ai_data for k in ("tweet", "cta", "hashtags", "keywords")):
            print("✖ Failed to generate required tweet content.")
            log_tweet(product_title, "", "", [], "", "gen_fail")
            notify_slack("ProductBot", "fail", "OpenAI generation failed.")
            return
        tweet_body = ai_data["tweet"].strip()
        tweet_cta = ai_data["cta"].strip()
        hashtags = ai_data.get("hashtags", [])
        keywords = ai_data.get("keywords", [])
        aff_link = generate_affiliate_link(keywords, product_title)
        final_tweet = format_generated_tweet(tweet_body, tweet_cta, hashtags, aff_link)
        twitter_client.create_tweet(text=final_tweet)
        log_tweet(product_title, tweet_body, tweet_cta, hashtags, aff_link, "success")
        print("[✓] Tweet posted successfully.")
        notify_slack("ProductBot", "success", f"Posted:\n{final_tweet}")
    except Exception as outer:
        print(f"[🔥 ERROR]: {outer}")
        notify_slack("ProductBot", "fail", f"Error:\n{str(outer)}")

if __name__ == "__main__":
    post_to_twitter()
