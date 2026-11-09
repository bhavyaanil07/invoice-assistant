import requests

import paypal_client as pp


def verify(headers, event, webhook_id):
    """Ask PayPal whether this webhook message really came from PayPal."""
    if not webhook_id:
        print("   [webhook] PAYPAL_WEBHOOK_ID is not set, rejecting")
        return False
    body = {
        "auth_algo": headers.get("paypal-auth-algo"),
        "cert_url": headers.get("paypal-cert-url"),
        "transmission_id": headers.get("paypal-transmission-id"),
        "transmission_sig": headers.get("paypal-transmission-sig"),
        "transmission_time": headers.get("paypal-transmission-time"),
        "webhook_id": webhook_id,
        "webhook_event": event,
    }
    if not all(body.values()):
        print("   [webhook] missing PayPal signature headers, rejecting")
        return False
    try:
        response = requests.post(
            f"{pp.BASE_URL}/v1/notifications/verify-webhook-signature",
            headers=pp.headers(pp.get_token()),
            json=body,
            timeout=20,
        )
        status = response.json().get("verification_status") if response.ok else None
        print(f"   [webhook] verification status: {status}")
        return status == "SUCCESS"
    except Exception as e:
        print(f"   [webhook] verification error: {e}")
        return False


def get_invoice(invoice_id):
    """Fetch one invoice's full details from PayPal."""
    try:
        response = requests.get(
            f"{pp.BASE_URL}/v2/invoicing/invoices/{invoice_id}",
            headers=pp.headers(pp.get_token()),
            timeout=20,
        )
        return response.json() if response.ok else None
    except Exception:
        return None