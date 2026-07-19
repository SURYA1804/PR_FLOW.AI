"""
Isolated email-sending service. Runs as its own FastAPI process so SMTP
credentials never live inside the core agent process — the agent only
knows this service's URL.

Run with: uvicorn app.mcp.email_server:app --port 8100
"""
import smtplib
from email.mime.text import MIMEText

from fastapi import FastAPI
from pydantic import BaseModel

from app.config import settings

app = FastAPI(title="Email notification service")


class EmailRequest(BaseModel):
    to: str
    subject: str
    body: str


@app.post("/send-email")
async def send_email(req: EmailRequest):
    msg = MIMEText(req.body)
    msg["Subject"] = req.subject
    msg["From"] = settings.SMTP_USER
    msg["To"] = req.to

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
        server.starttls()
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.send_message(msg)

    return {"status": "sent", "to": req.to}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
