"""
notifications/email_digest.py — Weekly HTML email digest.

Generates a clean ranked product list and sends it via SMTP.
Configure in config.EMAIL.
"""

import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from datetime             import date

from config import EMAIL, ROLLING_WINDOW_DAYS
import scorer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def send_digest(retailer=None):
    if not EMAIL.get('enabled'):
        logger.info('[email] Email digest disabled in config.')
        return

    required = ['smtp_host', 'username', 'password', 'from_address', 'to_addresses']
    for field in required:
        if not EMAIL.get(field):
            logger.error(f'[email] Missing config field: EMAIL["{field}"]')
            return

    top_n    = EMAIL.get('top_n', 20)
    products = scorer.get_rankings(limit=top_n, retailer=retailer)

    if not products:
        logger.info('[email] No products to report — skipping digest.')
        return

    html    = _build_html(products, top_n)
    subject = f'MRD Trend Tracker — Top {top_n} — {date.today().strftime("%d %b %Y")}'

    _send(subject, html)


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

def _build_html(products, top_n):
    rows_html = ''
    for i, p in enumerate(products, start=1):
        tags_html = ''.join(
            f'<span style="background:#e8f4fd;color:#1a6699;padding:2px 7px;'
            f'border-radius:10px;font-size:11px;margin-right:4px;">{t}</span>'
            for t in p.get('signal_tags', [])
        )
        price_str = f'£{p["latest_price"]:.2f}' if p.get('latest_price') else '—'
        rank_str  = f'#{p["latest_rank"]}' if p.get('latest_rank') else '—'
        score_str = f'{p["total_score"]:.0f} pts'
        days_str  = f'{p["days_tracked"]}d'

        # Product image (inline for email clients)
        img_html = ''
        if p.get('image_url'):
            img_html = (
                f'<img src="{p["image_url"]}" alt="{p["name"]}" width="56" height="56" '
                f'style="border-radius:6px;object-fit:cover;display:block;border:1px solid #f0f0f0;">'
            )
        else:
            img_html = (
                '<div style="width:56px;height:56px;background:#f4f4f2;border-radius:6px;'
                'display:flex;align-items:center;justify-content:center;'
                'font-size:22px;border:1px solid #f0f0f0;">&#128094;</div>'
            )

        rows_html += f'''
        <tr style="border-bottom:1px solid #f0f0f0;">
          <td style="padding:10px 8px;color:#888;font-size:13px;">{i}</td>
          <td style="padding:10px 8px;width:72px;">{img_html}</td>
          <td style="padding:10px 8px;">
            <a href="{p["url"]}" style="color:#1a1a1a;font-weight:500;text-decoration:none;">
              {p["name"]}
            </a><br>
            <span style="color:#888;font-size:12px;">{p.get("category","").replace("-"," ").title()}</span>
          </td>
          <td style="padding:10px 8px;font-size:13px;color:#555;">{price_str}</td>
          <td style="padding:10px 8px;font-size:13px;font-weight:600;color:#1a6699;">{score_str}</td>
          <td style="padding:10px 8px;font-size:12px;color:#888;">{rank_str} &nbsp; {days_str}</td>
          <td style="padding:10px 8px;">{tags_html}</td>
        </tr>'''

    return f'''<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>MRD Trend Tracker</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
             background:#f8f8f8;margin:0;padding:20px;">
  <div style="max-width:760px;margin:0 auto;background:#fff;
              border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.08);">

    <div style="background:#1a1a1a;padding:24px 28px;">
      <h1 style="color:#fff;margin:0;font-size:20px;font-weight:500;">MRD Trend Tracker</h1>
      <p style="color:#aaa;margin:4px 0 0;font-size:13px;">
        Top {top_n} products &nbsp;·&nbsp; {ROLLING_WINDOW_DAYS}-day rolling score &nbsp;·&nbsp;
        {date.today().strftime("%d %B %Y")}
      </p>
    </div>

    <div style="padding:0 28px 28px;">
      <table style="width:100%;border-collapse:collapse;margin-top:20px;">
        <thead>
          <tr style="border-bottom:2px solid #eee;">
            <th style="padding:8px;text-align:left;color:#888;font-size:12px;font-weight:500;">#</th>
            <th style="padding:8px;"></th>
            <th style="padding:8px;text-align:left;color:#888;font-size:12px;font-weight:500;">Product</th>
            <th style="padding:8px;text-align:left;color:#888;font-size:12px;font-weight:500;">Price</th>
            <th style="padding:8px;text-align:left;color:#888;font-size:12px;font-weight:500;">Score</th>
            <th style="padding:8px;text-align:left;color:#888;font-size:12px;font-weight:500;">Rank / Age</th>
            <th style="padding:8px;text-align:left;color:#888;font-size:12px;font-weight:500;">Signals</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>

    <div style="padding:16px 28px;background:#f8f8f8;border-top:1px solid #eee;">
      <p style="color:#aaa;font-size:11px;margin:0;">
        Generated automatically by MRD Trend Tracker.
        Scores accumulate over {ROLLING_WINDOW_DAYS} days.
        Higher scores indicate stronger signals of consumer interest.
      </p>
    </div>
  </div>
</body>
</html>'''


# ---------------------------------------------------------------------------
# SMTP sender
# ---------------------------------------------------------------------------

def _send(subject, html_body):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = EMAIL['from_address']
    msg['To']      = ', '.join(EMAIL['to_addresses'])
    msg.attach(MIMEText(html_body, 'html'))

    try:
        with smtplib.SMTP(EMAIL['smtp_host'], EMAIL['smtp_port']) as server:
            if EMAIL.get('use_tls', True):
                server.starttls()
            server.login(EMAIL['username'], EMAIL['password'])
            server.sendmail(
                EMAIL['from_address'],
                EMAIL['to_addresses'],
                msg.as_string(),
            )
        logger.info(f'[email] Digest sent to: {EMAIL["to_addresses"]}')
    except Exception as e:
        logger.error(f'[email] Failed to send: {e}')
