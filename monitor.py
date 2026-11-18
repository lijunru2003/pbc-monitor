"""Monitor People's Bank of China announcements and email matching updates."""

from __future__ import annotations

import json
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


ANNOUNCEMENTS_URL = (
    "http://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html"
)
STATE_FILE = Path("pbc_seen.json")
KEYWORDS = (
    "货币政策",
    "利率",
    "存款准备金率",
    "人民币",
    "金融市场",
    "征信",
    "支付",
    "外汇",
)


def fetch_announcements() -> list[dict[str, str]]:
    response = requests.get(
        ANNOUNCEMENTS_URL,
        headers={"User-Agent": "pbc-monitor/1.0"},
        timeout=30,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")
    announcements = []
    for link in soup.select("a[href]"):
        title = " ".join(link.get_text(" ", strip=True).split())
        href = urljoin(ANNOUNCEMENTS_URL, link["href"])
        if not title or not any(keyword in title for keyword in KEYWORDS):
            continue
        announcements.append({"title": title, "url": href})
    return announcements


def load_seen() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return set(data) if isinstance(data, list) else set()


def save_seen(seen: set[str]) -> None:
    STATE_FILE.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def send_email(announcements: list[dict[str, str]]) -> None:
    required = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "RECEIVER_EMAIL")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Missing required email secrets: {', '.join(missing)}")

    message = EmailMessage()
    message["Subject"] = f"PBC announcement update ({len(announcements)})"
    message["From"] = os.environ["SMTP_USER"]
    message["To"] = os.environ["RECEIVER_EMAIL"]
    message.set_content(
        "\n\n".join(f"{item['title']}\n{item['url']}" for item in announcements)
    )
    with smtplib.SMTP_SSL(
        os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"]), timeout=30
    ) as smtp:
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        smtp.send_message(message)


def main() -> None:
    seen = load_seen()
    announcements = fetch_announcements()
    new_announcements = [
        item for item in announcements if item["url"] not in seen
    ]
    seen.update(item["url"] for item in announcements)
    if new_announcements:
        send_email(new_announcements)
        print(f"Sent {len(new_announcements)} new announcement(s).")
    else:
        print("No new matching announcements.")
    save_seen(seen)


if __name__ == "__main__":
    main()
