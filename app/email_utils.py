"""
Sends the client their private gallery link + access key by email.

Works with any standard SMTP provider (Gmail app password, SendGrid,
Mailgun, Amazon SES, etc.) — just fill in the SMTP_* values in .env.
"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Your Studio")
STUDIO_NAME = os.getenv("STUDIO_NAME", "Your Studio")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")


def smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD)


def send_gallery_email(to_email: str, client_name: str, shoot_title: str, access_key: str) -> bool:
    gallery_link = f"{BASE_URL}/gallery/{access_key}"

    subject = f"Your photos from {shoot_title} are ready — {STUDIO_NAME}"
    body_text = (
        f"Hi {client_name},\n\n"
        f"Your photos from {shoot_title} are ready to view.\n\n"
        f"Open your gallery: {gallery_link}\n"
        f"Access key: {access_key}\n\n"
        "This link is private to you — please don't share it publicly.\n\n"
        f"Warmly,\n{STUDIO_NAME}"
    )
    body_html = f"""
    <div style="font-family: Georgia, serif; max-width: 520px; margin: auto; color:#1c1c1c;">
      <h2 style="font-weight:normal;">Hi {client_name},</h2>
      <p>Your photos from <strong>{shoot_title}</strong> are ready to view.</p>
      <p style="margin: 28px 0;">
        <a href="{gallery_link}"
           style="background:#1c1c1c;color:#f2ede4;padding:12px 22px;
                  text-decoration:none;border-radius:2px;font-family:sans-serif;">
          View your gallery
        </a>
      </p>
      <p style="font-size: 14px; color:#555;">
        Or use this private link and key directly:<br>
        Link: {gallery_link}<br>
        Access key: <strong>{access_key}</strong>
      </p>
      <p style="font-size: 13px; color:#888; margin-top: 32px;">
        This link is private to you — please don't share it publicly.
      </p>
      <p>Warmly,<br>{STUDIO_NAME}</p>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_USERNAME}>"
    msg["To"] = to_email
    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    if not smtp_configured():
        print(
            f"[email_utils] SMTP is not configured. "
            f"Set SMTP_USERNAME and SMTP_PASSWORD before sending to {to_email}."
        )
        return False

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_USERNAME, to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"[email_utils] Failed to send email: {e}")
        return False


def send_booking_confirmation(to_email: str, client_name: str, start_time_str: str) -> bool:
    subject = f"Appointment confirmed — {STUDIO_NAME}"
    body_html = f"""
    <div style="font-family: Georgia, serif; max-width: 520px; margin:auto; color:#1c1c1c;">
      <h2 style="font-weight:normal;">Hi {client_name},</h2>
      <p>Your appointment is confirmed for <strong>{start_time_str}</strong>.</p>
      <p>We'll send a reminder closer to the date. Reply to this email if you need to reschedule.</p>
      <p>Warmly,<br>{STUDIO_NAME}</p>
    </div>
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_USERNAME}>"
    msg["To"] = to_email
    msg.attach(MIMEText(body_html, "html"))

    if not SMTP_USERNAME or not SMTP_PASSWORD:
        print(f"[email_utils] SMTP not configured. Would send booking confirmation to {to_email}")
        return False

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_USERNAME, to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"[email_utils] Failed to send booking confirmation: {e}")
        return False
