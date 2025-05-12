import os
import requests
from datetime import datetime

def notify_slack(
    bot_name,
    status,
    message_block,
    trend=None,
    tweet=None,
    hashtag=None,
    context=None
):
    """Send a formatted Slack message with optional trend data."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    color = "#2eb886" if status.lower() == "success" else "#e01e5a"

    fields = [
        {
            "title": f"{bot_name} — {status.upper()}",
            "value": f"{message_block}\n🕒 {timestamp}",
            "short": False
        }
    ]

    if trend:
        fields.append({
            "title": "🧠 Trend Selected",
            "value": trend,
            "short": False
        })

    if tweet:
        fields.append({
            "title": "🐦 Tweet",
            "value": tweet.strip()[:280],
            "short": False
        })

    if hashtag:
        fields.append({
            "title": "🏷️ Hashtag",
            "value": hashtag,
            "short": True
        })

    if context:
        preview = "\n".join(context.splitlines()[:3])
        fields.append({
            "title": "🔍 Reddit Context Preview",
            "value": preview if preview else "(None)",
            "short": False
        })

    payload = { "attachments": [ { "fallback": f"{bot_name} update: {status}", "color": color, "fields": fields } ] }

    try:
        webhook_url = os.environ["SLACK_WEBHOOK_URL"]
        response = requests.post(webhook_url, json=payload)
        if response.status_code != 200:
            print(f"⚠️ Slack returned {response.status_code}: {response.text}")
        else:
            print("✅ Slack notified.")
    except Exception as e:
        print("❌ Slack notification failed:", e)
