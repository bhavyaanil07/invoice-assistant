import os
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

# Load keys from the .env file
load_dotenv()
CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
SECRET = os.getenv("PAYPAL_SECRET")
BASE_URL = os.getenv("PAYPAL_BASE_URL")
CLIENT_EMAIL = os.getenv("TEST_CLIENT_EMAIL")


def get_access_token():
    """Log in to PayPal with the app keys and get a temporary access token."""
    response = requests.post(
        f"{BASE_URL}/v1/oauth2/token",
        auth=(CLIENT_ID, SECRET),
        data={"grant_type": "client_credentials"},
    )
    if not response.ok:
        print("Could not log in to PayPal:", response.status_code, response.text)
        raise SystemExit
    return response.json()["access_token"]


def create_invoice(token, client_email, client_name, item, amount, due_in_days=7):
    """Create a draft invoice and return its ID."""
    due_date = (date.today() + timedelta(days=due_in_days)).isoformat()
    invoice = {
        "detail": {
            "currency_code": "USD",
            "note": "Thank you for your business!",
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
                "unit_amount": {"currency_code": "USD", "value": str(amount)},
            }
        ],
    }
    response = requests.post(
        f"{BASE_URL}/v2/invoicing/invoices",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
        json=invoice,
    )
    if not response.ok:
        print("Could not create invoice:", response.status_code, response.text)
        raise SystemExit
    return response.json()["id"]


def send_invoice(token, invoice_id):
    """Send the draft invoice to the client by email."""
    response = requests.post(
        f"{BASE_URL}/v2/invoicing/invoices/{invoice_id}/send",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"send_to_invoicer": True},
    )
    if not response.ok:
        print("Could not send invoice:", response.status_code, response.text)
        raise SystemExit
    return response.json()


def get_invoice(token, invoice_id):
    """Fetch the invoice to check its current status."""
    response = requests.get(
        f"{BASE_URL}/v2/invoicing/invoices/{invoice_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    print("1. Logging in to PayPal sandbox...")
    token = get_access_token()
    print("   Logged in.")

    print("2. Creating invoice...")
    invoice_id = create_invoice(token, CLIENT_EMAIL, "Rahul", "Logo design", 200)
    print("   Invoice ID:", invoice_id)

    print("3. Sending invoice...")
    send_result = send_invoice(token, invoice_id)
    print("   Sent. Payment link:", send_result.get("href"))

    print("4. Checking status...")
    invoice = get_invoice(token, invoice_id)
    print("   Status:", invoice["status"])
    print("   Amount:", invoice["amount"]["value"], invoice["amount"]["currency_code"])