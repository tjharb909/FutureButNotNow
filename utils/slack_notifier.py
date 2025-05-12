import os
import requests
from datetime import datetime

def notify_slack(bot_name, status, message_block):
    """Send a formatted Slack message using the webhook."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    color = "#2eb886" if status.lower() == "success" else "#e01e5a"

    payload = {
        "attachments": [
            {
                "fallback": f"{bot_name} update: {status}",
                "color": color,
                "fields": [
                    {
                        "title": f"{bot_name} — {status.upper()}",
                        "value": f"{message_block}\n🕒 {timestamp}",
                        "short": False
                    }
                ]
            }
        ]
    }

    try:
        webhook_url = os.environ["SLACK_WEBHOOK_URL"]
        response = requests.post(webhook_url, json=payload)
        if response.status_code != 200:
            print(f"⚠️ Slack returned {response.status_code}: {response.text}")
        else:
            print("✅ Slack notified.")
    except Exception as e:
        print("❌ Slack notification failed:", e)