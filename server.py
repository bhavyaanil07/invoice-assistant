import os
import re
import threading
import time
import uuid
from datetime import date, timedelta

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from google import genai
from google.genai import types

import paypal_client as pp
import risk
import webhooks

load_dotenv()
os.environ.pop("GOOGLE_API_KEY", None)
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "")
WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID", "")

MAX_INVOICE_AMOUNT = 10000
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DEPOSIT_PERCENT = 50
EARLY_DISCOUNT_PERCENT = 2
EARLY_DISCOUNT_DAYS = 2

app = FastAPI()
sessions = {}        # one chat per browser tab
payment_events = []  # payments reported by PayPal webhooks, shared by everyone viewing
events_lock = threading.Lock()
last_known_risk = {}  # client name -> latest risk result, used for before/after comparison


def remember_risk(result):
    if isinstance(result, dict) and "clients" in result:
        for c in result["clients"]:
            last_known_risk[c["client"]] = c


def risk_for(client_name):
    """Find a client's latest risk result, ignoring upper/lower case."""
    key = str(client_name).strip().lower()
    for name, c in last_known_risk.items():
        if name.lower() == key:
            return c
    remember_risk(risk.client_risk())
    for name, c in last_known_risk.items():
        if name.lower() == key:
            return c
    return None


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
  briefly mention their payment history, then propose the invoice.
- When you have the details, call propose_invoice. This does NOT send anything: the user sees a card
  with Send and Cancel buttons. Tell them to review the card. Do not ask "shall I send it?" in text.
- If propose_invoice returns protection_options, tell the user in one sentence that the card lets them
  protect the invoice, which option is recommended, and why (based on the client's risk).
- For reminders: call list_invoices to find the invoice and get_client_risk to check the client.
  For High risk clients write a clear, firm but polite reminder that mentions the due date and how late it is.
  For Low or Medium risk clients write a gentle, friendly reminder. Then call propose_reminder.
- When asked who owes money or about unpaid invoices, call list_invoices and answer briefly.
- When asked who pays late, who is risky, or when money will arrive, call get_client_risk and
  explain using its reason and expected_payment_date.
- If a tool returns an error, explain the problem to the user in simple words and ask for the correct
  details. Never retry with made-up values.
- Messages in [square brackets] come from the app, not the user typing. An [App notice] tells you
  about payments PayPal reported since the last message; take them into account.
- Never say an invoice or reminder was sent unless the app tells you PayPal confirmed it.
- Never make up invoice IDs, statuses or scores; always use the tools.
- Keep replies short and friendly. Write plain text only, no markdown, no asterisks.
"""


NUDGE_PROMPT = """[Automatic check when the app opens. The user did not type this.]
Call list_invoices. If any invoices are overdue, greet the user, say who is overdue and by how many days,
and offer to draft reminders, but do not propose them yet. If nothing is overdue, greet the user and give
a one-line summary of what is unpaid. Keep it to 2 or 3 sentences."""


# ---------- validation: checks the AI's values in code before anything reaches the user ----------

def validate_invoice(client_name, client_email, item, amount, due_date):
    problems = []
    if not str(client_name or "").strip():
        problems.append("the client name is missing")
    if not EMAIL_PATTERN.match(str(client_email or "").strip()):
        problems.append(f"'{client_email}' is not a valid email address")
    if not str(item or "").strip():
        problems.append("the item description is missing")
    try:
        value = float(amount)
        if value <= 0 or value > MAX_INVOICE_AMOUNT:
            problems.append(f"the amount must be between $0.01 and ${MAX_INVOICE_AMOUNT:,}")
    except (TypeError, ValueError):
        problems.append("the amount is not a number")
    try:
        due = date.fromisoformat(str(due_date)[:10])
        if due < date.today():
            problems.append("the due date is in the past")
        elif due > date.today() + timedelta(days=365):
            problems.append("the due date is more than a year away")
    except ValueError:
        problems.append("the due date must be in YYYY-MM-DD format")
    return problems


def validate_reminder(invoice_id, message):
    problems = []
    if not str(message or "").strip():
        problems.append("the reminder message is empty")
    elif len(message) > 1000:
        problems.append("the reminder message is too long")
    data = pp.list_invoices()
    if "error" in data:
        problems.append("could not check the invoice in PayPal")
        return problems
    match = next((i for i in data["invoices"] if i["invoice_id"] == invoice_id), None)
    if match is None:
        problems.append(f"invoice {invoice_id} does not exist in PayPal")
    elif match["status"] not in pp.UNPAID_STATUSES:
        problems.append(f"invoice {invoice_id} is already {match['status'].lower()}, so no reminder is needed")
    return problems


# ---------- invoice protection for risky clients ----------

def protection_options(amount, due_date):
    deposit = round(amount * DEPOSIT_PERCENT / 100, 2)
    discounted = round(amount * (100 - EARLY_DISCOUNT_PERCENT) / 100, 2)
    early_due = (date.today() + timedelta(days=EARLY_DISCOUNT_DAYS)).isoformat()
    return [
        {"key": "standard", "title": "Standard invoice",
         "detail": f"Full ${amount:.2f} due {due_date}"},
        {"key": "deposit", "title": f"Require {DEPOSIT_PERCENT}% upfront",
         "detail": f"PayPal requires a first payment of at least ${deposit:.2f}; the rest by {due_date}"},
        {"key": "early_discount", "title": f"{EARLY_DISCOUNT_PERCENT}% early-payment discount",
         "detail": f"Client pays ${discounted:.2f}, due {early_due}"},
    ]


# ---------- activity log: what the agent did, shown in the chat ----------

def log(state, kind, text):
    state["activity"].append({"kind": kind, "text": text})
    print(f"   [{kind}] {text}")


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
    with events_lock:
        latest_event = payment_events[-1]["id"] if payment_events else 0
    state = {"pending": {}, "new_actions": [], "activity": [], "model": MODEL, "seen_event_id": latest_event}

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
        problems = validate_invoice(client_name, client_email, item, amount, due_date)
        if problems:
            log(state, "error", "Validation blocked the invoice: " + "; ".join(problems))
            return {"error": "Validation failed: " + "; ".join(problems),
                    "what_to_do": "Explain this to the user and ask for the correct details."}
        action = {
            "id": uuid.uuid4().hex[:8],
            "type": "invoice",
            "client_name": client_name.strip(),
            "client_email": client_email.strip(),
            "item": item.strip(),
            "amount": round(float(amount), 2),
            "due_date": str(due_date)[:10],
        }
        result = {"status": "waiting_for_user_to_click_send", "action_id": action["id"]}

        client_risk = risk_for(action["client_name"])
        if client_risk and client_risk["risk_level"] in ("High", "Medium"):
            action["risk_level"] = client_risk["risk_level"]
            action["options"] = protection_options(action["amount"], action["due_date"])
            action["recommended"] = "deposit" if client_risk["risk_level"] == "High" else "early_discount"
            recommended_title = next(o["title"] for o in action["options"] if o["key"] == action["recommended"])
            log(state, "ai", f"Risk engine: {action['client_name']} is {client_risk['risk_level']} "
                             f"({client_risk['risk_score']}). Offering protection, recommended: {recommended_title}")
            result["protection_options"] = [o["title"] for o in action["options"]]
            result["recommended"] = recommended_title
            result["client_risk_reason"] = client_risk["reason"]

        state["pending"][action["id"]] = action
        state["new_actions"].append(action)
        log(state, "wait", f"Invoice checked and ready for {action['client_name']}: "
                           f"${action['amount']:.2f}, due {action['due_date']}. Waiting for your approval")
        return result

    def propose_reminder(invoice_id: str, client_name: str, message: str) -> dict:
        """Prepare a payment reminder for the user to review. This does NOT send anything.
        The user will see a card with Send and Cancel buttons.

        Args:
            invoice_id: The PayPal invoice ID from list_invoices.
            client_name: The client's name.
            message: A short, polite reminder message written for the client.
        """
        problems = validate_reminder(invoice_id, message)
        if problems:
            log(state, "error", "Validation blocked the reminder: " + "; ".join(problems))
            return {"error": "Validation failed: " + "; ".join(problems),
                    "what_to_do": "Explain this to the user. Use list_invoices to find the right invoice."}
        action = {
            "id": uuid.uuid4().hex[:8],
            "type": "reminder",
            "invoice_id": invoice_id,
            "client_name": client_name,
            "message": message.strip(),
        }
        state["pending"][action["id"]] = action
        state["new_actions"].append(action)
        log(state, "wait", f"Reminder for {client_name} checked against PayPal and ready. Waiting for your approval")
        return {"status": "waiting_for_user_to_click_send", "action_id": action["id"]}

    def list_invoices() -> dict:
        """Get all recent invoices with client, amount, status, due date and whether they are overdue."""
        start = time.time()
        result = pp.list_invoices()
        if "error" in result:
            log(state, "error", "PayPal: could not read invoices")
        else:
            invoices = result["invoices"]
            overdue = sum(1 for i in invoices if i["overdue"])
            log(state, "read", f"PayPal: read {len(invoices)} invoices ({overdue} overdue) in {time.time() - start:.1f}s")
        return result

    def get_client_risk() -> dict:
        """Score each client's late-payment risk (Low, Medium, High or New) from their PayPal payment
        history, with a reason, and predict when each open invoice is likely to be paid."""
        start = time.time()
        result = risk.client_risk()
        if "error" in result:
            log(state, "error", "Risk engine: could not score clients")
        else:
            remember_risk(result)
            summary = ", ".join(
                f"{c['client']} {c['risk_level']}" + (f" ({c['risk_score']})" if c["risk_score"] is not None else "")
                for c in result["clients"]
            )
            log(state, "ai", f"Risk engine: {summary} in {time.time() - start:.1f}s")
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


def switch_model(state, model, reason):
    """Move the conversation to another model, keeping the chat history."""
    history = state["chat"].get_history()
    state["chat"] = make_chat(model, state["tools"], history)
    state["model"] = model
    log(state, "info", f"{reason}, switched to {model}")


def ask_agent(state, text, pre_activity=None):
    state["activity"] = list(pre_activity or [])
    start = time.time()
    reply = None
    for attempt in range(1, 4):
        state["new_actions"] = []
        # keep earlier notes, drop tool lines from a failed attempt
        state["activity"] = [a for a in state["activity"] if a["kind"] in ("info", "user", "paypal")]
        try:
            response = state["chat"].send_message(text)
            reply = response.text or ""
            break
        except Exception as e:
            print(f"   [warning] Attempt {attempt} with {state['model']} failed: {e}")
            on_backup = state["model"] != MODEL
            if on_backup:
                switch_model(state, MODEL, "Backup model failed")
            elif not is_busy_error(e):
                reply = "Sorry, something went wrong on my side. Please try again."
                break

            if attempt == 1:
                time.sleep(2)
            elif attempt == 2:
                if FALLBACK_MODEL and FALLBACK_MODEL != MODEL:
                    switch_model(state, FALLBACK_MODEL, "Main model busy")
                else:
                    time.sleep(4)
    if reply is None:
        reply = "The AI service is very busy right now. Please try again in a few seconds."
    print(f"   [timing] Full AI reply took {time.time() - start:.1f} seconds ({state['model']})")
    return {"reply": reply, "actions": state["new_actions"], "activity": state["activity"]}


def pending_notices(state):
    """Payments reported since this chat last heard about them, as text for the agent."""
    with events_lock:
        new_events = [e for e in payment_events if e["id"] > state["seen_event_id"]]
    if not new_events:
        return ""
    state["seen_event_id"] = new_events[-1]["id"]
    return "[App notice: " + " ".join(e["message"] for e in new_events) + "]\n"


# ---------- PayPal webhook: payment detected ----------

def describe_timing(paid_on, due_date):
    try:
        days = (date.fromisoformat(paid_on[:10]) - date.fromisoformat(due_date[:10])).days
    except (TypeError, ValueError):
        return ""
    if days > 0:
        return f"{days} day{'s' if days != 1 else ''} late"
    if days < 0:
        return f"{-days} day{'s' if days != -1 else ''} early"
    return "on the due date"


def risk_text(c):
    if c is None:
        return "unknown"
    if c["risk_score"] is None:
        return "New"
    return f"{c['risk_level']} ({c['risk_score']})"


def handle_webhook(event, headers):
    if not webhooks.verify(headers, event, WEBHOOK_ID):
        return 400, {"status": "rejected"}

    event_type = event.get("event_type", "")
    resource = event.get("resource", {}) or {}
    invoice = resource.get("invoice", resource)
    invoice_id = invoice.get("id")
    print(f"   [webhook] verified {event_type} for {invoice_id}")

    if event_type != "INVOICING.INVOICE.PAID" or not invoice_id:
        return 200, {"status": "ignored"}
    with events_lock:
        if any(e["invoice_id"] == invoice_id for e in payment_events):
            return 200, {"status": "duplicate"}

    details = webhooks.get_invoice(invoice_id) or invoice
    recipient = (details.get("primary_recipients") or [{}])[0].get("billing_info", {})
    client_name = recipient.get("name", {}).get("given_name") or recipient.get("name", {}).get("full_name") or "A client"
    amount = details.get("amount", {})
    amount_text = f"${float(amount.get('value', 0)):.2f}"
    due_date = details.get("detail", {}).get("payment_term", {}).get("due_date", "")
    transactions = details.get("payments", {}).get("transactions", [])
    paid_on = (transactions[-1].get("payment_date") if transactions else None) or date.today().isoformat()

    before = last_known_risk.get(client_name)
    after_all = risk.client_risk()
    remember_risk(after_all)
    after = next((c for c in after_all.get("clients", []) if c["client"] == client_name), None)

    timing = describe_timing(paid_on, due_date)
    message = f"Payment detected: {client_name} paid {amount_text} for invoice {invoice_id}"
    message += f", {timing}." if timing else "."
    if after is not None:
        if before is not None and before.get("avg_days_late") is not None:
            message += (f" Risk {risk_text(before)} \u2192 {risk_text(after)}; average lateness "
                        f"{before['avg_days_late']} \u2192 {after['avg_days_late']} days.")
        else:
            message += f" Risk is now {risk_text(after)}."

    with events_lock:
        payment_events.append({
            "id": (payment_events[-1]["id"] + 1) if payment_events else 1,
            "event_type": event_type,
            "invoice_id": invoice_id,
            "client": client_name,
            "message": message,
        })
    print(f"   [webhook] {message}")
    return 200, {"status": "ok"}


# ---------- start-up warm-up so the first visitor doesn't wait ----------

@app.on_event("startup")
def warm_up():
    def run():
        try:
            remember_risk(risk.client_risk())
            print("   [info] Risk cache warmed up")
        except Exception as e:
            print(f"   [info] Warm-up failed: {e}")
    threading.Thread(target=run, daemon=True).start()


# ---------- web endpoints ----------

class ChatIn(BaseModel):
    session_id: str
    message: str


class SessionIn(BaseModel):
    session_id: str


class ConfirmIn(BaseModel):
    session_id: str
    action_id: str
    approved: bool
    option: str = "standard"


@app.api_route("/", methods=["GET", "HEAD"])
def home():
    return FileResponse("static/index.html")


@app.post("/api/chat")
def chat(body: ChatIn):
    state = get_session(body.session_id)
    return ask_agent(state, pending_notices(state) + body.message)


@app.post("/api/nudge")
def nudge(body: SessionIn):
    return ask_agent(get_session(body.session_id), NUDGE_PROMPT)


@app.post("/api/confirm")
def confirm(body: ConfirmIn):
    state = get_session(body.session_id)
    action = state["pending"].pop(body.action_id, None)
    if action is None:
        return {"reply": "That action is no longer available.", "actions": [], "activity": [], "result": None}

    pre = []
    if not body.approved:
        pre.append({"kind": "user", "text": f"You cancelled the {action['type']} for {action['client_name']}. Nothing was sent"})
        note = f"[The user clicked Cancel on the {action['type']} for {action['client_name']}. Nothing was sent.] Acknowledge briefly."
        out = ask_agent(state, note, pre)
        out["result"] = None
        return out

    start = time.time()
    if action["type"] == "invoice":
        allowed = [o["key"] for o in action.get("options", [])]
        option = body.option if body.option in allowed else "standard"
        due_date = action["due_date"]
        deposit = discount = None
        if option == "deposit":
            deposit = DEPOSIT_PERCENT
        elif option == "early_discount":
            discount = EARLY_DISCOUNT_PERCENT
            due_date = (date.today() + timedelta(days=EARLY_DISCOUNT_DAYS)).isoformat()
        chosen = next((o["title"] for o in action.get("options", []) if o["key"] == option), "Standard invoice")
        pre.append({"kind": "user", "text": f"You approved the invoice for {action['client_name']} ({chosen})"})

        result = pp.create_and_send_invoice(
            action["client_name"], action["client_email"], action["item"],
            action["amount"], due_date,
            deposit_percent=deposit, discount_percent=discount,
        )
        if "error" in result:
            pre.append({"kind": "error", "text": "PayPal: invoice failed. " + result["error"][:160]})
        else:
            extra = "" if result.get("protection") == "standard" else f" with {result['protection']}"
            pre.append({"kind": "paypal", "text": f"PayPal: invoice {result['invoice_id']} created{extra} and sent "
                                                  f"in {time.time() - start:.1f}s"})
    else:
        pre.append({"kind": "user", "text": f"You approved the reminder for {action['client_name']}"})
        result = pp.send_reminder(action["invoice_id"], action["message"])
        if "error" in result:
            pre.append({"kind": "error", "text": "PayPal: reminder failed. " + result["error"][:160]})
        else:
            pre.append({"kind": "paypal", "text": f"PayPal: reminder sent for {action['invoice_id']} "
                                                  f"in {time.time() - start:.1f}s"})

    note = (f"[The user clicked Send on the {action['type']}. PayPal result: {result}] "
            "Tell the user briefly what happened, including any protection applied. "
            "If there is a payment link, include it.")
    out = ask_agent(state, note, pre)
    out["result"] = result
    return out


@app.post("/api/paypal/webhook")
async def paypal_webhook(request: Request):
    try:
        event = await request.json()
    except Exception:
        return JSONResponse({"status": "bad request"}, status_code=400)
    headers = {k.lower(): v for k, v in request.headers.items()}
    status_code, body = await run_in_threadpool(handle_webhook, event, headers)
    return JSONResponse(body, status_code=status_code)


@app.get("/api/events")
def events(after: int = 0):
    with events_lock:
        return {"events": [e for e in payment_events if e["id"] > after]}


@app.get("/api/invoices")
def invoices():
    return pp.list_invoices()


@app.get("/api/risk")
def client_risk():
    result = risk.client_risk()
    remember_risk(result)
    return result