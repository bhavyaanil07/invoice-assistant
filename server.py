import os
import uuid
from datetime import date

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel
from google import genai
from google.genai import types

import paypal_client as pp

load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

app = FastAPI()
sessions = {}  # one chat per browser tab


def system_prompt():
    today = date.today()
    return f"""
You are PayNudge, an invoicing assistant for freelancers and small sellers, connected to PayPal.
Today is {today.strftime('%A')}, {today.isoformat()}. Use this to work out dates like "Friday" or "next week".
All amounts are in US dollars.

How you work:
- To bill someone you need: client name, client email, item, amount and due date.
  If anything is missing, ask for it. If no due date is given, use 7 days from today.
- When you have the details, call propose_invoice. This does NOT send anything: the user sees a card
  with Send and Cancel buttons. Tell them to review the card. Do not ask "shall I send it?" in text.
- For reminders: call list_invoices to find the invoice, write a short, polite reminder in a warm tone,
  then call propose_reminder. The user confirms with a button.
- When asked who owes money or about unpaid invoices, call list_invoices and answer briefly.
- Messages in [square brackets] come from the app, not the user typing.
- Never say an invoice or reminder was sent unless the app tells you PayPal confirmed it.
- Never make up invoice IDs or statuses; always use the tools.
- Keep replies short and friendly. Write plain text only, no markdown, no asterisks.
"""


NUDGE_PROMPT = """[Automatic check when the app opens. The user did not type this.]
Call list_invoices. If any invoices are overdue, greet the user, say who is overdue and by how many days,
and offer to draft reminders, but do not propose them yet. If nothing is overdue, greet the user and give
a one-line summary of what is unpaid. Keep it to 2 or 3 sentences."""


def new_session():
    state = {"pending": {}, "new_actions": []}

    def propose_invoice(client_name: str, client_email: str, item: str,
                        amount: float, due_date: str) -> dict:
        """Prepare an invoice for the user to review. This does NOT send anything.
        The user will see a card with Send and Cancel buttons.

        Args:
            client_name: The client's first name, e.g. "Rahul".
            client_email: The client's email address.
            item: What the invoice is for, e.g. "Logo design".
            amount: The amount in US dollars, e.g. 200.
            due_date: The due date in YYYY-MM-DD format.
        """
        action = {
            "id": uuid.uuid4().hex[:8],
            "type": "invoice",
            "client_name": client_name,
            "client_email": client_email,
            "item": item,
            "amount": amount,
            "due_date": due_date,
        }
        state["pending"][action["id"]] = action
        state["new_actions"].append(action)
        return {"status": "waiting_for_user_to_click_send", "action_id": action["id"]}

    def propose_reminder(invoice_id: str, client_name: str, message: str) -> dict:
        """Prepare a payment reminder for the user to review. This does NOT send anything.
        The user will see a card with Send and Cancel buttons.

        Args:
            invoice_id: The PayPal invoice ID from list_invoices.
            client_name: The client's name.
            message: A short, polite reminder message written for the client.
        """
        action = {
            "id": uuid.uuid4().hex[:8],
            "type": "reminder",
            "invoice_id": invoice_id,
            "client_name": client_name,
            "message": message,
        }
        state["pending"][action["id"]] = action
        state["new_actions"].append(action)
        return {"status": "waiting_for_user_to_click_send", "action_id": action["id"]}

    def list_invoices() -> dict:
        """Get all recent invoices with client, amount, status, due date and whether they are overdue."""
        return pp.list_invoices()

    state["chat"] = client.chats.create(
        model=MODEL,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt(),
            tools=[propose_invoice, propose_reminder, list_invoices],
        ),
    )
    return state


def get_session(session_id):
    if session_id not in sessions:
        sessions[session_id] = new_session()
    return sessions[session_id]


def ask_agent(state, text):
    state["new_actions"] = []
    try:
        response = state["chat"].send_message(text)
        reply = response.text or ""
    except Exception as e:
        reply = f"Sorry, something went wrong: {e}"
    return {"reply": reply, "actions": state["new_actions"]}


class ChatIn(BaseModel):
    session_id: str
    message: str


class SessionIn(BaseModel):
    session_id: str


class ConfirmIn(BaseModel):
    session_id: str
    action_id: str
    approved: bool


@app.get("/")
def home():
    return FileResponse("static/index.html")


@app.post("/api/chat")
def chat(body: ChatIn):
    return ask_agent(get_session(body.session_id), body.message)


@app.post("/api/nudge")
def nudge(body: SessionIn):
    return ask_agent(get_session(body.session_id), NUDGE_PROMPT)


@app.post("/api/confirm")
def confirm(body: ConfirmIn):
    state = get_session(body.session_id)
    action = state["pending"].pop(body.action_id, None)
    if action is None:
        return {"reply": "That action is no longer available.", "actions": [], "result": None}

    if not body.approved:
        note = f"[The user clicked Cancel on the {action['type']} for {action['client_name']}. Nothing was sent.] Acknowledge briefly."
        out = ask_agent(state, note)
        out["result"] = None
        return out

    if action["type"] == "invoice":
        result = pp.create_and_send_invoice(
            action["client_name"], action["client_email"], action["item"],
            action["amount"], action["due_date"],
        )
    else:
        result = pp.send_reminder(action["invoice_id"], action["message"])

    note = (f"[The user clicked Send on the {action['type']}. PayPal result: {result}] "
            "Tell the user briefly what happened. If there is a payment link, include it.")
    out = ask_agent(state, note)
    out["result"] = result
    return out


@app.get("/api/invoices")
def invoices():
    return pp.list_invoices()