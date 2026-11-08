import os
from datetime import date

import requests
from dotenv import load_dotenv

load_dotenv()
CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
SECRET = os.getenv("PAYPAL_SECRET")
BASE_URL = os.getenv("PAYPAL_BASE_URL")

UNPAID_STATUSES = {"SENT", "UNPAID", "PARTIALLY_PAID", "PAYMENT_PENDING"}


def get_token():
    """Log in to PayPal and return an access token."""
    response = requests.post(
        f"{BASE_URL}/v1/oauth2/token",
        auth=(CLIENT_ID, SECRET),
        data={"grant_type": "client_credentials"},
    )
    response.raise_for_status()
    return response.json()["access_token"]


def headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def create_and_send_invoice(client_name, client_email, item, amount, due_date,
                            note="Thank you for your business!"):
    """Create a PayPal invoice, send it, and return a short summary."""
    try:
        token = get_token()
        invoice = {
            "detail": {
                "currency_code": "USD",
                "note": note,
                "payment_term": {"due_date": due_date},
            },
            "primary_recipients": [
                {
                    "billing_info": {
                        "email_address": client_email,
                        "name": {"given_name": client_name},
                    }
                }
            ],
            "items": [
                {
                    "name": item,
                    "quantity": "1",
                    "unit_amount": {"currency_code": "USD", "value": f"{float(amount):.2f}"},
                }
            ],
        }
        create = requests.post(
            f"{BASE_URL}/v2/invoicing/invoices",
            headers={**headers(token), "Prefer": "return=representation"},
            json=invoice,
        )
        if not create.ok:
            return {"error": f"Could not create invoice: {create.text}"}
        invoice_id = create.json()["id"]

        send = requests.post(
            f"{BASE_URL}/v2/invoicing/invoices/{invoice_id}/send",
            headers=headers(token),
            json={"send_to_invoicer": True},
        )
        if not send.ok:
            return {"error": f"Invoice {invoice_id} created but not sent: {send.text}"}

        return {
            "invoice_id": invoice_id,
            "status": "SENT",
            "client": client_name,
            "amount": f"{float(amount):.2f} USD",
            "due_date": due_date,
            "payment_link": send.json().get("href"),
        }
    except Exception as e:
        return {"error": str(e)}


def list_invoices():
    """Return a simple list of recent invoices with status and overdue flag."""
    try:
        token = get_token()
        response = requests.get(
            f"{BASE_URL}/v2/invoicing/invoices",
            headers=headers(token),
            params={"page": 1, "page_size": 50},
        )
        if not response.ok:
            return {"error": f"Could not list invoices: {response.text}"}

        today = date.today().isoformat()
        results = []
        for inv in response.json().get("items", []):
            status = inv.get("status", "")
            if status in ("DRAFT", "CANCELLED"):
                continue
            recipient = (inv.get("primary_recipients") or [{}])[0].get("billing_info", {})
            name = recipient.get("name", {}).get("given_name") or recipient.get("name", {}).get("full_name") or "Unknown"
            due_date = inv.get("detail", {}).get("payment_term", {}).get("due_date", "")
            results.append({
                "invoice_id": inv.get("id"),
                "client": name,
                "client_email": recipient.get("email_address", ""),
                "amount": f'{inv.get("amount", {}).get("value", "?")} {inv.get("amount", {}).get("currency_code", "")}',
                "status": status,
                "due_date": due_date,
                "overdue": status in UNPAID_STATUSES and bool(due_date) and due_date < today,
            })
        return {"today": today, "invoices": results}
    except Exception as e:
        return {"error": str(e)}


def send_reminder(invoice_id, message):
    """Send a payment reminder for an unpaid invoice through PayPal."""
    try:
        token = get_token()
        response = requests.post(
            f"{BASE_URL}/v2/invoicing/invoices/{invoice_id}/remind",
            headers=headers(token),
            json={
                "subject": "Friendly payment reminder",
                "note": message,
                "send_to_invoicer": False,
                "send_to_recipient": True,
            },
        )
        if not response.ok:
            return {"error": f"Could not send reminder: {response.text}"}
        return {"invoice_id": invoice_id, "reminder_sent": True, "message": message}
    except Exception as e:
        return {"error": str(e)}