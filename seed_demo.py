import os
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

import paypal_client as pp

load_dotenv()
CLIENT_EMAIL = os.getenv("TEST_CLIENT_EMAIL")
BASE_URL = pp.BASE_URL
today = date.today()

# (client, item, amount, due date in days from today, paid how many days after due date)
# paid = None means still unpaid. Negative paid means paid before the due date.
DEMO_INVOICES = [
    ("Rahul", "Logo design", 180, -75, 8),
    ("Rahul", "Poster set", 120, -45, 12),
    ("Rahul", "Brand guide", 250, -20, 5),
    ("Rahul", "Social media kit", 160, -5, None),
    ("Priya", "Website copy", 300, -70, -2),
    ("Priya", "Blog articles", 200, -40, -1),
    ("Priya", "Newsletter", 150, -15, 0),
    ("Priya", "Landing page copy", 220, 10, None),
    ("Arjun", "App icons", 90, -50, 0),
    ("Arjun", "UI screens", 400, -25, 3),
    ("Arjun", "Pitch deck design", 350, -2, None),
    ("Meera", "Product photos", 275, 12, None),
]


def create_demo_invoice(token, client, item, amount, due_offset, paid_offset):
    due_date = today + timedelta(days=due_offset)
    invoice_date = due_date - timedelta(days=14)
    invoice = {
        "detail": {
            "currency_code": "USD",
            "invoice_date": invoice_date.isoformat(),
            "note": "Thank you for your business!",
            "payment_term": {"due_date": due_date.isoformat()},
        },
        "primary_recipients": [
            {
                "billing_info": {
                    "email_address": CLIENT_EMAIL,
                    "name": {"given_name": client},
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

    create = requests.post(
        f"{BASE_URL}/v2/invoicing/invoices",
        headers={**pp.headers(token), "Prefer": "return=representation"},
        json=invoice,
    )
    if not create.ok:
        print(f"  FAILED to create {client} - {item}: {create.text}")
        return
    invoice_id = create.json()["id"]

    send = requests.post(
        f"{BASE_URL}/v2/invoicing/invoices/{invoice_id}/send",
        headers=pp.headers(token),
        json={"send_to_invoicer": True},
    )
    if not send.ok:
        print(f"  FAILED to send {client} - {item}: {send.text}")
        return

    if paid_offset is None:
        print(f"  {client:6} {item:20} ${amount:>4}  due {due_date}  -> left unpaid")
        return

    payment_date = due_date + timedelta(days=paid_offset)
    pay = requests.post(
        f"{BASE_URL}/v2/invoicing/invoices/{invoice_id}/payments",
        headers=pp.headers(token),
        json={
            "method": "BANK_TRANSFER",
            "payment_date": payment_date.isoformat(),
            "amount": {"currency_code": "USD", "value": f"{amount:.2f}"},
            "note": "Demo payment history",
        },
    )
    if not pay.ok:
        print(f"  FAILED to record payment for {client} - {item}: {pay.text}")
        return
    print(f"  {client:6} {item:20} ${amount:>4}  due {due_date}  -> paid {payment_date}")


if __name__ == "__main__":
    print("Creating demo invoices in PayPal sandbox...")
    token = pp.get_token()
    for row in DEMO_INVOICES:
        create_demo_invoice(token, *row)
    print("Done.")