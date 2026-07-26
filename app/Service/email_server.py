"""
Isolated email-sending service. Runs as its own FastAPI process so SMTP
credentials never live inside the core agent process — the agent only
knows this service's URL.

Run with: uvicorn app.mcp.email_server:app --port 8100
"""
import smtplib
from email.mime.text import MIMEText

from pydantic import BaseModel

from app.config import settings



# class EmailRequest(BaseModel):
#     to: str
#     subject: str
#     body: str


async def send_email(to: str, subject: str, body: str):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_USER
    msg["To"] = to

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
        server.starttls()
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.send_message(msg)

    return {"status": "sent", "to": req.to}



