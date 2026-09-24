import os
import requests

PAYSTACK_SECRET = os.environ.get("PAYSTACK_SECRET_KEY", "")


def _headers():
    return {
        "Authorization": f"Bearer {PAYSTACK_SECRET}",
        "Content-Type": "application/json",
    }


def list_banks() -> list:
    r = requests.get(
        "https://api.paystack.co/bank?country=nigeria&currency=NGN",
        headers=_headers(),
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("data", [])


def create_recipient(name: str, account_number: str, bank_code: str) -> dict:
    r = requests.post(
        "https://api.paystack.co/transferrecipient",
        headers=_headers(),
        json={
            "type": "nuban",
            "name": name,
            "account_number": account_number,
            "bank_code": bank_code,
            "currency": "NGN",
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("data", {})


def initiate_transfer(recipient_code: str, amount_naira: float, reason: str) -> dict:
    r = requests.post(
        "https://api.paystack.co/transfer",
        headers=_headers(),
        json={
            "source": "balance",
            "reason": reason,
            "amount": int(amount_naira * 100),   # kobo
            "recipient": recipient_code,
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("data", {})
