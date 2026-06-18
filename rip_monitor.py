import imaplib
import smtplib
import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
import requests
import re
from datetime import datetime

# ============================================================
# CONFIGURATION — fill in your details here
# ============================================================
import os
GMAIL_ADDRESS   = os.environ.get("GMAIL_ADDRESS", "")
APP_PASSWORD    = os.environ.get("APP_PASSWORD", "")
NOTIFY_EMAIL    = os.environ.get("NOTIFY_EMAIL", "")
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    )
}

KNOWN_STREAMERS = {
    "eventlive":        "EventLive",
    "churchservices":   "Church Services TV",
    "youtube":          "YouTube",
    "facebook":         "Facebook Live",
    "mcnmedia":         "MCN Media",
    "funeralvideo":     "Funeral Video IE",
    "vimeo":            "Vimeo",
    "obitus":           "Obitus",
    "tributestream":    "Tribute Stream",
}


def identify_streamer(url):
    """Look at a livestream URL and identify the platform/operator."""
    url_lower = url.lower()
    for keyword, name in KNOWN_STREAMERS.items():
        if keyword in url_lower:
            return name
    # Try fetching the page title as a fallback
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        title = soup.find("title")
        if title:
            return f"Unknown ({title.text.strip()[:60]})"
    except Exception:
        pass
    return "Unknown streamer"


def check_notice_for_livestream(notice_url):
    """Visit a rip.ie death notice page and look for a livestream link."""
    try:
        r = requests.get(notice_url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        # Find all links in the page
        links = soup.find_all("a", href=True)

        # Look for livestream mentions in surrounding text or link text
        livestream_links = []
        for link in links:
            href = link["href"]
            text = link.get_text(strip=True).lower()
            context = ""
            if link.parent:
                context = link.parent.get_text(strip=True).lower()

            is_stream = (
                "livestream" in text or
                "live stream" in text or
                "live" in text and "stream" in context or
                "click here" in text and "stream" in context or
                "watch" in text and "live" in context or
                any(k in href.lower() for k in KNOWN_STREAMERS.keys())
            )

        if is_stream and href.startswith("http"):
        if "youtube.com/@Rip.ieEndofLifeMatters" not in href:
        livestream_links.append(href)

        # Also scan raw page text for livestream mentions
        page_text = soup.get_text().lower()
        has_mention = "livestream" in page_text or "live stream" in page_text

        return livestream_links, has_mention

    except Exception as e:
        return [], False


def get_rip_links_from_email(msg):
    """Extract name and rip.ie notice URL from a RIP.ie alert email."""
    results = []
    body = ""

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                break
    else:
        body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

    soup = BeautifulSoup(body, "html.parser")

    # Find all links pointing to rip.ie death notices
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "rip.ie/death-notice/" in href and "/s/" not in href:
            name = a.get_text(strip=True)
            if name:
                results.append({"name": name, "url": href})

    return results


def fetch_rip_emails():
    """Log into Gmail via IMAP and fetch unread RIP.ie alert emails."""
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_ADDRESS, APP_PASSWORD)
    mail.select("inbox")

    # Search for unread emails from rip.ie
    status, messages = mail.search(None, '(UNSEEN FROM "rip.ie")')
    email_ids = messages[0].split()

    fetched = []
    for eid in email_ids:
        status, data = mail.fetch(eid, "(RFC822)")
        raw = data[0][1]
        msg = email.message_from_bytes(raw)
        fetched.append(msg)
        # Mark as read
        mail.store(eid, "+FLAGS", "\\Seen")

    mail.logout()
    return fetched


def send_summary_email(findings):
    """Send a formatted summary email with the results."""
    today = datetime.now().strftime("%d %B %Y")

    if not findings:
        subject = f"RIP.ie Monitor — {today} — No livestreams found"
        body = f"<h2>RIP.ie Monitor — {today}</h2><p>No new notices with livestreams found today.</p>"
    else:
        subject = f"RIP.ie Monitor — {today} — {len(findings)} livestream(s) found"
        rows = ""
        for f in findings:
            stream_info = ""
            if f["streams"]:
                for url in f["streams"]:
                    streamer = identify_streamer(url)
                    stream_info += f'<br>🎥 <b>{streamer}</b>: <a href="{url}">{url}</a>'
            else:
                stream_info = "<i>Livestream mentioned but no direct link found</i>"

            rows += f"""
            <tr>
                <td style="padding:8px;border-bottom:1px solid #eee;">
                    <a href="{f['notice_url']}">{f['name']}</a>
                </td>
                <td style="padding:8px;border-bottom:1px solid #eee;">
                    {stream_info}
                </td>
            </tr>"""

        body = f"""
        <h2 style="color:#8B6914;">RIP.ie Monitor — {today}</h2>
        <p>{len(findings)} notice(s) with livestream found in your area:</p>
        <table style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;">
            <tr style="background:#c8a951;color:white;">
                <th style="padding:8px;text-align:left;">Name</th>
                <th style="padding:8px;text-align:left;">Livestream</th>
            </tr>
            {rows}
        </table>
        <p style="color:#999;font-size:12px;margin-top:20px;">
            Sent automatically by your RIP Monitor script
        </p>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, NOTIFY_EMAIL, msg.as_string())

    print(f"Summary email sent: {subject}")


def main():
    print(f"RIP Monitor starting — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    emails = fetch_rip_emails()
    print(f"Found {len(emails)} new RIP.ie alert email(s)")

    all_notices = []
    for msg in emails:
        notices = get_rip_links_from_email(msg)
        all_notices.extend(notices)

    print(f"Found {len(all_notices)} death notice link(s) to check")

    findings = []
    for notice in all_notices:
        print(f"  Checking: {notice['name']} — {notice['url']}")
        streams, has_mention = check_notice_for_livestream(notice["url"])
        if streams or has_mention:
            findings.append({
                "name":       notice["name"],
                "notice_url": notice["url"],
                "streams":    streams,
            })
            print(f"    → Livestream found! {streams}")
        else:
            print(f"    → No livestream")

    send_summary_email(findings)
    print("Done.")


if __name__ == "__main__":
    main()
