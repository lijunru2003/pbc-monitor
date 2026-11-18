import html
import json
import os
import re
import smtplib
import time
from datetime import datetime, timezone, timedelta
from email.header import Header
from email.mime.text import MIMEText
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

TARGET_URL = "https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html"
HISTORY_FILE = "pbc_seen.json"

# ================= 关键词过滤配置 =================
# 测试完记得改回：TITLE_KEYWORDS = ["公告", "计划"] 和 BODY_KEYWORDS = ["纪念币"]
TITLE_KEYWORDS = ["互换", "协议"]
BODY_KEYWORDS = ["协议"]
# ==================================================

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL", SMTP_USER)


def get_beijing_time() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")


def load_history() -> set:
    if not os.path.exists(HISTORY_FILE):
        return set()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_history(seen_urls: set):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen_urls), f, ensure_ascii=False, indent=2)


def matches_any(text: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    return any(kw in text for kw in keywords)


def goto_with_retry(page, url: str, wait_selector: str | None = None, retries: int = 3):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            print(f"正在访问 ({attempt}/{retries}): {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if wait_selector:
                page.wait_for_selector(wait_selector, timeout=30000)
            return
        except Exception as e:
            last_err = e
            print(f"第 {attempt} 次访问失败: {e}")
            if attempt < retries:
                time.sleep(5)
    raise last_err


def html_to_text(raw_html: str) -> str:
    """从详情页 HTML 中提取纯文本（优先截取 #zoom 正文区域）"""
    match = re.search(r'<div[^>]*id=["\']zoom["\'][^>]*>(.*?)</div>', raw_html, re.S | re.I)
    segment = match.group(1) if match else raw_html
    segment = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", segment, flags=re.S | re.I)
    segment = re.sub(r"<(br|p|/p|div|/div|li|/li)[^>]*>", "\n", segment, flags=re.I)
    text = re.sub(r"<[^>]+>", "", segment)
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def extract_body_text(context, page, url: str) -> str:
    """优先复用浏览器 Cookie 直接 HTTP 请求详情页，速度更快；失败时再降级浏览器打开"""
    for attempt in range(1, 3):
        try:
            print(f"快速拉取正文 ({attempt}/2): {url}")
            resp = context.request.get(url, timeout=30000)
            if resp.ok:
                raw_html = resp.text()
                text = html_to_text(raw_html)
                if len(text) > 30:
                    return text
        except Exception as e:
            print(f"快速拉取失败 ({attempt}/2): {e}")
            time.sleep(3)

    # 降级为浏览器页面打开
    goto_with_retry(page, url, wait_selector="#zoom, .content, body", retries=2)
    for selector in ("#zoom", ".content", "body"):
        locator = page.locator(selector).first
        if locator.count() > 0:
            text = locator.inner_text().strip()
            if text:
                return text
    return ""


def check_updates(seen_urls: set, is_first_run: bool) -> tuple[list[dict], list[dict], set]:
    all_items = []
    matched_items = []
    failed_urls = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()

        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in {"image", "media", "font", "stylesheet"}
            else route.continue_()
        )

        try:
            goto_with_retry(page, TARGET_URL, wait_selector="a[href*='/113469/']", retries=3)

            links = page.locator(".newslist_style a").all()
            if not links:
                links = page.locator("a[href*='/goutongjiaoliu/113456/113469/']").all()

            seen_in_page = set()
            for link in links:
                href = link.get_attribute("href")
                if not href or not re.search(r"/\d+/index\.html", href):
                    continue

                full_url = urljoin(TARGET_URL, href)
                if full_url in seen_in_page:
                    continue
                seen_in_page.add(full_url)

                title = (link.get_attribute("title") or link.inner_text() or "").strip()
                if title:
                    all_items.append({"title": title, "url": full_url})

            if is_first_run:
                return all_items, [], failed_urls

            for item in all_items:
                if item["url"] in seen_urls:
                    continue

                if not matches_any(item["title"], TITLE_KEYWORDS):
                    print(f"[跳过 - 标题不匹配] {item['title']}")
                    continue

                if BODY_KEYWORDS:
                    try:
                        body_text = extract_body_text(context, page, item["url"])
                    except Exception as e:
                        print(f"[正文抓取失败，留待下次重试] {item['title']} | {e}")
                        failed_urls.add(item["url"])
                        continue

                    if not matches_any(body_text, BODY_KEYWORDS):
                        print(f"[跳过 - 正文不匹配] {item['title']}")
                        continue
                    item["body"] = body_text
                else:
                    item["body"] = ""

                print(f"[命中推送] {item['title']}")
                matched_items.append(item)

        finally:
            browser.close()

    return all_items, matched_items, failed_urls


def send_email(new_items: list[dict]):
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS, RECEIVER_EMAIL]):
        print("未完整配置 SMTP 环境变量，跳过发送邮件。")
        return

    now = get_beijing_time()
    blocks = []
    for item in new_items:
        safe_title = html.escape(item["title"])
        raw_body = item.get("body", "")
        preview = raw_body[:1500] + ("……\n（正文较长，请点击标题查看原文）" if len(raw_body) > 1500 else "")
        safe_body = html.escape(preview).replace("\n", "<br>")

        blocks.append(f"""
        <div style="margin-bottom: 20px; padding: 14px; background: #f8f9fa; border-left: 4px solid #1a73e8; border-radius: 4px;">
          <h4 style="margin: 0 0 8px 0;">
            <a href="{item['url']}" target="_blank" style="color: #1a73e8; text-decoration: none;">{safe_title}</a>
          </h4>
          <div style="font-size: 13px; color: #333; line-height: 1.7;">{safe_body}</div>
        </div>
        """)

    html_body = f"""
    <div style="font-family: sans-serif; line-height: 1.6;">
      <h3>中国人民银行公告更新提醒（关键词命中）</h3>
      <p style="color: #666; font-size: 13px;">
        检测时间（北京时间）：{now}<br>
        过滤规则：标题含「{" / ".join(TITLE_KEYWORDS)}」且正文含「{" / ".join(BODY_KEYWORDS)}」
      </p>
      {"".join(blocks)}
      <hr style="border: none; border-top: 1px solid #eee;">
      <p style="font-size: 12px; color: #999;">来源页面：<a href="{TARGET_URL}">{TARGET_URL}</a></p>
    </div>
    """

    msg = MIMEText(html_body, "html", "utf-8")
    msg["From"] = SMTP_USER
    msg["To"] = RECEIVER_EMAIL
    subject = f"【央行纪念币相关公告】{new_items[0]['title'][:28]}"
    if len(new_items) > 1:
        subject += f" 等 {len(new_items)} 条"
    msg["Subject"] = Header(subject, "utf-8")

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [RECEIVER_EMAIL], msg.as_string())
    print(f"邮件发送成功，共推送 {len(new_items)} 条命中公告。")


def main():
    is_first_run = not os.path.exists(HISTORY_FILE)
    seen_urls = load_history()

    all_items, matched_items, failed_urls = check_updates(seen_urls, is_first_run)
    if not all_items:
        raise RuntimeError("未提取到任何公告，可能触发了反爬或海外节点访问超时。")

    has_new_url = False
    for item in all_items:
        # 详情页抓取失败的链接不记入已读，下次自动重试
        if item["url"] in failed_urls:
            continue
        if item["url"] not in seen_urls:
            seen_urls.add(item["url"])
            has_new_url = True

    if is_first_run:
        save_history(seen_urls)
        print(f"首次运行初始化完成，已记录 {len(seen_urls)} 条现有公告。")
        return

    if has_new_url:
        save_history(seen_urls)

    if matched_items:
        send_email(matched_items)
    else:
        print(f"本次扫描 {len(all_items)} 条，无符合关键词条件的新公告。")


if __name__ == "__main__":
    main()
