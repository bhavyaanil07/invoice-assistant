import os
import time
from datetime import date

import requests
from dotenv import load_dotenv

load_dotenv()
CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
SECRET = os.getenv("PAYPAL_SECRET")
BASE_URL = os.getenv("PAYPAL_BASE_URL")

UNPAID_STATUSES = {"SENT", "UNPAID", "PARTIALLY_PAID", "PAYMENT_PENDING"}

_token_cache = {"token": None, "expires_at": 0}


def get_token():
    """Log in to PayPal and return an access token, reusing it until it expires."""
    if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"]
    response = requests.post(
        f"{BASE_URL}/v1/oauth2/token",
        auth=(CLIENT_ID, SECRET),
        data={"grant_type": "client_credentials"},
    )
    response.raise_for_status()
    data = response.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
    return _token_cache["token"]


def headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def create_and_send_invoice(client_name, client_email, item, amount, due_date,
                            note="Thank you for your business!",
                            deposit_percent=None, discount_percent=None):
    """Create a PayPal invoice, send it, and return a short summary.

    deposit_percent: if set (e.g. 50), PayPal requires at least this share as the first payment.
    discount_percent: if set (e.g. 2), PayPal applies this discount to the whole invoice.
    """
    try:
        token = get_token()
        amount = float(amount)
        notes = [note]
        invoice = {
            "detail": {
                "currency_code": "USD",
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
                    "unit_amount": {"currency_code": "USD", "value": f"{amount:.2f}"},
                }
            ],
        }

        protection = "standard"
        if deposit_percent:
            minimum = round(amount * deposit_percent / 100, 2)
            invoice["configuration"] = {
                "partial_payment": {
                    "allow_partial_payment": True,
                    "minimum_amount_due": {"currency_code": "USD", "value": f"{minimum:.2f}"},
                }
            }
            notes.append(f"A first payment of at least ${minimum:.2f} ({deposit_percent}%) is required.")
            protection = f"{deposit_percent}% minimum upfront payment (${minimum:.2f})"
        elif discount_percent:
            invoice["amount"] = {
                "breakdown": {
                    "discount": {"invoice_discount": {"percent": str(discount_percent)}}
                }
            }
            notes.append(f"Includes a {discount_percent}% early-payment discount.")
            protection = f"{discount_percent}% early-payment discount"

        invoice["detail"]["note"] = " ".join(notes)

        create = requests.post(
            f"{BASE_URL}/v2/invoicing/invoices",
            headers={**headers(token), "Prefer": "return=representation"},
            json=invoice,
        )
        if not create.ok:
            return {"error": f"Could not create invoice: {create.text}"}
        created = create.json()
        invoice_id = created["id"]
        total = created.get("amount", {}).get("value", f"{amount:.2f}")

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
            "amount": f"{float(total):.2f} USD",
            "due_date": due_date,
            "protection": protection,
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