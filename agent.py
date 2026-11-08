import os
from datetime import date

from dotenv import load_dotenv
from google import genai
from google.genai import types

import paypal_client as pp

load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")


# These are the tools the AI is allowed to use.
# The docstrings tell the AI what each tool does.

def create_and_send_invoice(client_name: str, client_email: str, item: str,
                            amount: float, due_date: str) -> dict:
    """Create and send a PayPal invoice to a client.

    Args:
        client_name: The client's first name, e.g. "Rahul".
        client_email: The client's email address.
        item: What the invoice is for, e.g. "Logo design".
        amount: The amount in US dollars, e.g. 200.
        due_date: The due date in YYYY-MM-DD format.
    """
    print(f"   [PayPal] Creating and sending invoice to {client_name}...")
    return pp.create_and_send_invoice(client_name, client_email, item, amount, due_date)


def list_invoices() -> dict:
    """Get all recent invoices with their status, due date and whether they are overdue."""
    print("   [PayPal] Fetching invoices...")
    return pp.list_invoices()


def send_reminder(invoice_id: str, message: str) -> dict:
    """Send a polite payment reminder for one unpaid invoice.

    Args:
        invoice_id: The PayPal invoice ID, e.g. "INV2-XXXX-XXXX-XXXX-XXXX".
        message: A short, polite reminder message written for the client.
    """
    print(f"   [PayPal] Sending reminder for {invoice_id}...")
    return pp.send_reminder(invoice_id, message)


today = date.today()
SYSTEM_PROMPT = f"""
You are an invoicing assistant for freelancers and small sellers, connected to PayPal.
Today is {today.strftime('%A')}, {today.isoformat()}. Use this to work out dates like "Friday" or "next week".
All amounts are in US dollars.

Rules:
- To create an invoice you need: client name, client email, item, amount and due date.
  If anything is missing, ask for it. If no due date is given, use 7 days from today.
- Before creating an invoice, show the details in a short summary and ask "Shall I send it?".
  Only call create_and_send_invoice after the user clearly says yes.
- Before sending a reminder, show the reminder message you wrote and ask for confirmation.
- When asked who owes money or about unpaid invoices, use list_invoices and give a short, clear answer.
- After sending an invoice, share the payment link.
- Keep replies short and friendly. Never make up invoice IDs or statuses; always use the tools.
"""

chat = client.chats.create(
    model=MODEL,
    config=types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[create_and_send_invoice, list_invoices, send_reminder],
    ),
)

print("Invoice Assistant is ready. Type 'quit' to exit.\n")
while True:
    user_text = input("You: ").strip()
    if not user_text:
        continue
    if user_text.lower() in ("quit", "exit"):
        break
    try:
        response = chat.send_message(user_text)
        print("Assistant:", response.text, "\n")
    except Exception as e:
        print("Error:", e, "\n")