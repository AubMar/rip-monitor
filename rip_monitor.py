import imaplib
import smtplib
import email
import os
import sqlite3
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
import requests
from datetime import datetime

# ============================================================
# CONFIGURATION — reads from GitHub Secrets (environment variables)
# ============================================================
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
APP_PASSWORD  = os.environ.get("APP_PASSWORD", "")
NOTIFY_EMAIL  = os.environ.get("NOTIFY_EMAIL", "")
# ============================================================

DB_PATH = "processed_notices.db"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    )
}

KNOWN_STREAMERS = {
    "eventlive":      "EventLive",
    "churchservices": "Church Services TV",
    "youtube":        "YouTube",
    "facebook":       "Facebook Live",
    "mcnmedia":       "MCN Media",
    "funeralvideo":   "Funeral Video IE",
    "vimeo":          "Vimeo",
    "obitus":         "Obitus",
    "tributestream":  "Tribute Stream",
}

IGNORE_URLS = [
    "youtube.com/@Rip.ieEndofLifeMatters",
    "youtube.com/rip.ie",
]


# ------------------------------------------------------------
# DATABASE
# ------------------------------------------------------------
def init_db():
    """Create the database table if it doesn't already exist."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_notices (
            notice_url   TEXT PRIMARY KEY,
            name         TEXT,
            date_found   TEXT,
            had_stream   INTEGER
        )
    """)
    conn.commit()
    return conn


def already_processed(conn, notice_url):
    """Return True if this notice_url already exists in the database."""
    cursor = conn.execute(
        "SELECT 1 FROM processed_notices WHERE notice_url = ?",
        (notice_url,)
    )
    return cursor.fetchone() is not None


def mark_processed(conn, notice_url, name, had_stream):
    """Insert a record so this notice is never reprocessed."""
    conn.execute(
        "INSERT OR IGNORE INTO processed_notices (notice_url, name, date_found, had_stream) "
        "VALUES (?, ?, ?, ?)",
        (notice_url, name, datetime.now().strftime("%Y-%m-%d %H:%M"), int(had_stream))
    )
    conn.commit()


# ------------------------------------------------------------
# STREAM DETECTION
# ------------------------------------------------------------
def identify_streamer(url):
    url_lower = url.lower()
    for keyword, name in KNOWN_STREAMERS.items():
        if keyword in url_lower:
            return name
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        title = soup.find("title")
        if title:
            return f"Unknown ({title.text.strip()[:60]})"
    except Exception:
        pass
    return "Unknown streamer"


def is_ignored_url(url):
    return any(pattern in url for pattern in IGNORE_URLS)


def check_notice_for_livestream(notice_url):
    try:
        r = requests.get(notice_url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        links = soup.find_all("a", href=True)
        livestream_links = []

        for link in links:
            href = link["href"]
            text = link.get_text(strip=True).lower()
            context = ""
            if link.parent:
                context = link.parent.get_text(strip=True).lower()

            is_stream = (
                "livestream" in text
                or "live stream" in text
                or ("live" in text and "stream" in context)
                or ("click here" in text and "stream" in context)
                or ("watch" in text and "live" in context)
                or any(k in href.lower() for k in KNOWN_STREAMERS.keys())
            )

            if is_stream and href.startswith("http"):
                if not is_ignored_url(href):
                    livestream_links.append(href)

        page_text = soup.get_text().lower()
        has_mention = "livestream" in page_text or "live stream" in page_text

        return livestream_links, has_mention

    except Exception:
        return [], False


# ------------------------------------------------------------
# EMAIL READING (Gmail is NEVER marked as read — purely informational)
# ------------------------------------------------------------
def get_rip_links_from_email(msg):
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

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "rip.ie/death-notice/" in href and "/s/" not in href:
            name = a.get_text(strip=True)
            if name:
                results.append({"name": name, "url": href})

    return results


def fetch_rip_emails():
    """
    Reads ALL rip.ie emails (read or unread) from recent history.
    Does NOT mark anything as read - Gmail's read/unread flag is left
    entirely under Aubrey's manual control.
    Deduplication is handled separately via the SQLite database.
    """
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_ADDRESS, APP_PASSWORD)
    mail.select("inbox", readonly=True)  # readonly=True guarantees nothing gets altered

    status, messages = mail.search(None, '(FROM "rip.ie")')
    email_ids = messages[0].split()

    # Limit to the most recent 150 to keep things fast
    email_ids = email_ids[-150:]

    fetched = []
    for eid in email_ids:
        status, data = mail.fetch(eid, "(RFC822)")
        raw = data[0][1]
        msg = email.message_from_bytes(raw)
        fetched.append(msg)
        # No mail.store() call - nothing is ever marked as read

    mail.logout()
    return fetched


# ------------------------------------------------------------
# SUMMARY EMAIL
# ------------------------------------------------------------
def send_summary_email(findings):
    today = datetime.now().strftime("%d %B %Y")

    if not findings:
        subject = f"RIP.ie Monitor — {today} — No new livestreams found"
        body = f"<h2>RIP.ie Monitor — {today}</h2><p>No new notices with livestreams found.</p>"
    else:
        subject = f"RIP.ie Monitor — {today} — {len(findings)} new livestream(s) found"
        rows = ""
        for f in findings:
            stream_info = ""
            if f["streams"]:
                for url in f["streams"]:
                    streamer = identify_streamer(url)
                    stream_info += f'<br>streamer: <b>{streamer}</b>: <a href="{url}">{url}</a>'
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
        <p>{len(findings)} new notice(s) with livestream found:</p>
        <table style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;">
            <tr style="background:#c8a951;color:white;">
                <th style="padding:8px;text-align:left;">Name</th>
                <th style="padding:8px;text-align:left;">Livestream</th>
            </tr>
            {rows}
        </table>
        <p style="color:#999;font-size:12px;margin-top:20px;">
            Sent automatically by your RIP Monitor — each notice is reported once only
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


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------
def main():
    print(f"RIP Monitor starting — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    conn = init_db()

    emails = fetch_rip_emails()
    print(f"Found {len(emails)} RIP.ie email(s) in inbox (read or unread)")

    all_notices = []
    for msg in emails:
        notices = get_rip_links_from_email(msg)
        all_notices.extend(notices)

    # De-duplicate notices that appear in multiple emails this run
    seen_this_run = set()
    unique_notices = []
    for n in all_notices:
        if n["url"] not in seen_this_run:
            seen_this_run.add(n["url"])
            unique_notices.append(n)

    print(f"Found {len(unique_notices)} unique death notice link(s)")

    # Filter out ones already in the database
    new_notices = [n for n in unique_notices if not already_processed(conn, n["url"])]
    print(f"{len(new_notices)} of those are new (not yet in database)")

    findings = []
    for notice in new_notices:
        print(f"  Checking: {notice['name']} — {notice['url']}")
        streams, has_mention = check_notice_for_livestream(notice["url"])
        had_stream = bool(streams or has_mention)

        if had_stream:
            findings.append({
                "name":       notice["name"],
                "notice_url": notice["url"],
                "streams":    streams,
            })
            print(f"    -> Livestream found! {streams}")
        else:
            print(f"    -> No livestream")

        # Record in the database either way, so it's never checked again
        mark_processed(conn, notice["url"], notice["name"], had_stream)

    send_summary_email(findings)
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
