"""
Cliente para a API Asaas (pagamentos).
Suporta ambiente sandbox e produção.
"""

import os
import requests
from datetime import date, timedelta

ASAAS_SANDBOX_URL = "https://sandbox.asaas.com/api/v3"
ASAAS_PROD_URL = "https://www.asaas.com/api/v3"

CONFIRMED_STATUSES = {"RECEIVED", "CONFIRMED"}


def _base_url() -> str:
    env = os.environ.get("ASAAS_ENVIRONMENT", "sandbox").lower()
    return ASAAS_SANDBOX_URL if env == "sandbox" else ASAAS_PROD_URL


def _headers() -> dict:
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "access_token": os.environ.get("ASAAS_API_KEY", ""),
    }


def create_customer(name: str, email: str) -> dict:
    """Cria ou recupera um cliente no Asaas pelo e-mail."""
    # Tenta buscar cliente existente com o mesmo e-mail
    resp = requests.get(
        f"{_base_url()}/customers",
        params={"email": email},
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("data"):
        return data["data"][0]

    # Cria novo cliente
    resp = requests.post(
        f"{_base_url()}/customers",
        json={"name": name, "email": email},
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def create_payment(customer_id: str, value: float, description: str, external_reference: str = "") -> dict:
    """
    Cria uma cobrança para o cliente.
    billingType UNDEFINED permite que o cliente escolha PIX, boleto ou cartão.
    Retorna o objeto de pagamento do Asaas (contém invoiceUrl).
    """
    due_date = (date.today() + timedelta(days=3)).isoformat()
    resp = requests.post(
        f"{_base_url()}/payments",
        json={
            "customer": customer_id,
            "billingType": "UNDEFINED",
            "value": value,
            "dueDate": due_date,
            "description": description,
            "externalReference": external_reference,
        },
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_payment(payment_id: str) -> dict:
    """Retorna os detalhes de uma cobrança pelo ID."""
    resp = requests.get(
        f"{_base_url()}/payments/{payment_id}",
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def is_payment_confirmed(payment_id: str) -> bool:
    """Retorna True se a cobrança foi paga/confirmada."""
    try:
        payment = get_payment(payment_id)
        return payment.get("status") in CONFIRMED_STATUSES
    except Exception:
        return False
