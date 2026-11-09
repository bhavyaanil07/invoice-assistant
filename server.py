import os
import time
import uuid
from datetime import date

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel
from google import genai
from google.genai import types

import paypal_client as pp
import risk

load_dotenv()
os.environ.pop("GOOGLE_API_KEY", None)
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "")

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
- Before proposing an invoice for an existing client, call get_client_risk. If the client is High risk,
  briefly mention their payment history and suggest a shorter due date, then propose the invoice anyway.
- When you have the details, call propose_invoice. This does NOT send anything: the user sees a card
  with Send and Cancel buttons. Tell them to review the card. Do not ask "shall I send it?" in text.
- For reminders: call list_invoices to find the invoice and get_client_risk to check the client.
  For High risk clients write a clear, firm but polite reminder that mentions the due date and how late it is.
  For Low or Medium risk clients write a gentle, friendly reminder. Then call propose_reminder.
- When asked who owes money or about unpaid invoices, call list_invoices and answer briefly.
- When asked who pays late, who is risky, or when money will arrive, call get_client_risk and
  explain using its reason and expected_payment_date.
- Messages in [square brackets] come from the app, not the user typing.
- Never say an invoice or reminder was sent unless the app tells you PayPal confirmed it.
- Never make up invoice IDs, statuses or scores; always use the tools.
- Keep replies short and friendly. Write plain text only, no markdown, no asterisks.
"""


NUDGE_PROMPT = """[Automatic check when the app opens. The user did not type this.]
Call list_invoices. If any invoices are overdue, greet the user, say who is overdue and by how many days,
and offer to draft reminders, but do not propose them yet. If nothing is overdue, greet the user and give
a one-line summary of what is unpaid. Keep it to 2 or 3 sentences."""


def make_chat(model, tools, history=None):
    """Create a Gemini chat. Low thinking is only used on Gemini 3 models, which support it."""
    config_args = {"system_instruction": system_prompt(), "tools": tools}
    if model.startswith("gemini-3"):
        config_args["thinking_config"] = types.ThinkingConfig(thinking_level="low")
    return client.chats.create(
        model=model,
        config=types.GenerateContentConfig(**config_args),
        history=history or [],
    )


def new_session():
    state = {"pending": {}, "new_actions": [], "model": MODEL}

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
        start = time.time()
        result = pp.list_invoices()
        print(f"   [timing] PayPal list_invoices took {time.time() - start:.1f} seconds")
        return result

    def get_client_risk() -> dict:
        """Score each client's late-payment risk (Low, Medium, High or New) from their PayPal payment
        history, with a reason, and predict when each open invoice is likely to be paid."""
        start = time.time()
        result = risk.client_risk()
        print(f"   [timing] Risk scoring took {time.time() - start:.1f} seconds")
        return result

    state["tools"] = [propose_invoice, propose_reminder, list_invoices, get_client_risk]
    state["chat"] = make_chat(MODEL, state["tools"])
    return state


def get_session(session_id):
    if session_id not in sessions:
        sessions[session_id] = new_session()
    return sessions[session_id]


def is_busy_error(error):
    text = str(error)
    return any(code in text for code in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "overloaded"))


def switch_to_fallback(state):
    """Move the conversation to the backup model, keeping the chat history."""
    if not FALLBACK_MODEL or state["model"] == FALLBACK_MODEL:
        return False
    history = state["chat"].get_history()
    state["chat"] = make_chat(FALLBACK_MODEL, state["tools"], history)
    state["model"] = FALLBACK_MODEL
    print(f"   [info] Switched to backup model {FALLBACK_MODEL}")
    return True


def ask_agent(state, text):
    start = time.time()
    reply = None
    for attempt in range(1, 4):
        state["new_actions"] = []
        try:
            response = state["chat"].send_message(text)
            reply = response.text or ""
            break
        except Exception as e:
            print(f"   [warning] Attempt {attempt} with {state['model']} failed: {e}")
            if not is_busy_error(e):
                reply = "Sorry, something went wrong on my side. Please try again."
                break
            if attempt == 1:
                time.sleep(2)
            elif attempt == 2:
                if not switch_to_fallback(state):
                    time.sleep(4)
    if reply is None:
        reply = "The AI service is very busy right now. Please try again in a few seconds."
    print(f"   [timing] Full AI reply took {time.time() - start:.1f} seconds ({state['model']})")
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


@app.api_route("/", methods=["GET", "HEAD"])
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


@app.get("/api/risk")
def client_risk():
    return risk.client_risk()
