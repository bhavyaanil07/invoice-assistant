from datetime import date, timedelta

import requests

import paypal_client as pp

PAID_STATUSES = {"PAID", "MARKED_AS_PAID"}
_payment_date_cache = {}  # paid invoices never change, so we only fetch each one once


def _to_date(text):
    return date.fromisoformat(text[:10])


def _payment_date(token, invoice_id):
    """Look up when a paid invoice was actually paid."""
    if invoice_id in _payment_date_cache:
        return _payment_date_cache[invoice_id]
    response = requests.get(
        f"{pp.BASE_URL}/v2/invoicing/invoices/{invoice_id}",
        headers=pp.headers(token),
    )
    if not response.ok:
        return None
    transactions = response.json().get("payments", {}).get("transactions", [])
    if not transactions:
        return None
    paid_on = (transactions[-1].get("payment_date") or "")[:10] or None
    _payment_date_cache[invoice_id] = paid_on
    return paid_on


def client_risk():
    """Score each client's late-payment risk and predict when open invoices will be paid."""
    try:
        data = pp.list_invoices()
        if "error" in data:
            return data
        token = pp.get_token()
        today = date.today()

        clients = {}
        for inv in data["invoices"]:
            c = clients.setdefault(inv["client"], {"paid": 0, "late": 0, "delays": [], "open": []})
            if not inv["due_date"]:
                continue
            due = _to_date(inv["due_date"])
            if inv["status"] in PAID_STATUSES:
                paid_on = _payment_date(token, inv["invoice_id"])
                if not paid_on:
                    continue
                delay = (_to_date(paid_on) - due).days
                c["paid"] += 1
                c["delays"].append(max(0, delay))
                if delay > 0:
                    c["late"] += 1
            elif inv["status"] in pp.UNPAID_STATUSES:
                c["open"].append(inv)

        results = []
        for name, c in clients.items():
            avg_delay = sum(c["delays"]) / len(c["delays"]) if c["delays"] else 0
            late_ratio = c["late"] / c["paid"] if c["paid"] else 0
            overdue_now = [i for i in c["open"] if i["overdue"]]
            days_overdue = max((today - _to_date(i["due_date"])).days for i in overdue_now) if overdue_now else 0

            if c["paid"] == 0 and not overdue_now:
                level, score = "New", None
                reason = "No payment history yet."
            else:
                score = round(50 * late_ratio + 30 * min(avg_delay, 15) / 15 + (20 if overdue_now else 0))
                level = "High" if score >= 60 else "Medium" if score >= 30 else "Low"
                if c["paid"]:
                    reason = f"Paid {c['late']} of {c['paid']} invoices late, {avg_delay:.1f} days late on average."
                else:
                    reason = "No paid invoices yet."
                if overdue_now:
                    reason += f" Currently {days_overdue} days overdue."

            predictions = []
            for inv in c["open"]:
                expected = _to_date(inv["due_date"]) + timedelta(days=round(avg_delay))
                if expected < today:
                    expected = today + timedelta(days=1)
                predictions.append({
                    "invoice_id": inv["invoice_id"],
                    "amount": inv["amount"],
                    "due_date": inv["due_date"],
                    "expected_payment_date": expected.isoformat(),
                })

            results.append({
                "client": name,
                "risk_level": level,
                "risk_score": score,
                "paid_invoices": c["paid"],
                "paid_late": c["late"],
                "avg_days_late": round(avg_delay, 1),
                "currently_overdue_days": days_overdue,
                "reason": reason,
                "open_invoices": predictions,
            })

        results.sort(key=lambda r: -1 if r["risk_score"] is None else r["risk_score"], reverse=True)
        return {"today": today.isoformat(), "clients": results}
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    import json
    print(json.dumps(client_risk(), indent=2))