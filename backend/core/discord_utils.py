# backend/core/discord_utils.py
import os
import requests
import logging

logger = logging.getLogger(__name__)

def send_discord_alert(title: str, message: str, target: str = "admin"):
    """
    Sends a formatted embed alert to a Discord webhook.
    target: 'admin' or 'agent'
    """
    if target == "admin":
        webhook_url = os.getenv("DISCORD_ADMIN_WEBHOOK_URL")
        color = 15158332  # Red/Orange for admin alerts
    else:
        webhook_url = os.getenv("DISCORD_AGENT_WEBHOOK_URL")
        color = 3066993   # Green for agent alerts

    if not webhook_url:
        logger.warning("Discord webhook URL not set in environment variables.")
        return

    payload = {
        "embeds": [{
            "title": title,
            "description": message,
            "color": color,
            "footer": {
                "text": "PrintHub Kabale University"
            }
        }]
    }

    try:
        # timeout=3 ensures it fails fast and doesn't block the user's request
        requests.post(webhook_url, json=payload, timeout=3)
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send Discord alert: {e}")
