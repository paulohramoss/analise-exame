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


def _raise_for_status(resp: requests.Response) -> None:
    """Levanta exceção com o corpo da resposta Asaas incluído na mensagem."""
    if not resp.ok:
        try:
            body = resp.json()
            errors = body.get("errors", [])
            msg = "; ".join(e.get("description", str(e)) for e in errors) if errors else str(body)
        except Exception:
            msg = resp.text[:300]
        raise requests.HTTPError(
            f"{resp.status_code} {resp.reason} — {msg}",
            response=resp,
        )


def create_customer(name: str, email: str, cpf_cnpj: str = "") -> dict:
    """Cria ou recupera um cliente no Asaas pelo e-mail.
    Se já existir sem CPF/CNPJ, atualiza o registro antes de retornar.
    """
    cpf_cnpj = cpf_cnpj.strip()

    # Busca cliente existente pelo e-mail
    resp = requests.get(
        f"{_base_url()}/customers",
        params={"email": email},
        headers=_headers(),
        timeout=10,
    )
    _raise_for_status(resp)
    data = resp.json()

    if data.get("data"):
        customer = data["data"][0]
        # Se o cliente existente não tem CPF/CNPJ e foi fornecido um, atualiza
        if cpf_cnpj and not customer.get("cpfCnpj"):
            upd = requests.put(
                f"{_base_url()}/customers/{customer['id']}",
                json={"cpfCnpj": cpf_cnpj},
                headers=_headers(),
                timeout=10,
            )
            _raise_for_status(upd)
            return upd.json()
        return customer

    # Cria novo cliente
    payload: dict = {"name": name, "email": email}
    if cpf_cnpj:
        payload["cpfCnpj"] = cpf_cnpj

    resp = requests.post(
        f"{_base_url()}/customers",
        json=payload,
        headers=_headers(),
        timeout=10,
    )
    _raise_for_status(resp)
    return resp.json()


BILLING_TYPES = {"PIX", "BOLETO", "CREDIT_CARD", "UNDEFINED"}


def create_payment(
    customer_id: str,
    value: float,
    description: str,
    external_reference: str = "",
    billing_type: str = "UNDEFINED",
    credit_card: dict | None = None,
    credit_card_holder_info: dict | None = None,
) -> dict:
    """
    Cria uma cobrança para o cliente.
    billing_type: PIX | BOLETO | CREDIT_CARD | UNDEFINED.
    Para CREDIT_CARD, passe credit_card e credit_card_holder_info.
    Retorna o objeto de pagamento do Asaas.
    """
    if billing_type not in BILLING_TYPES:
        billing_type = "UNDEFINED"
    due_date = (date.today() + timedelta(days=3)).isoformat()
    payload: dict = {
        "customer": customer_id,
        "billingType": billing_type,
        "value": value,
        "dueDate": due_date,
        "description": description,
        "externalReference": external_reference,
    }
    if billing_type == "CREDIT_CARD" and credit_card:
        payload["creditCard"] = credit_card
    if billing_type == "CREDIT_CARD" and credit_card_holder_info:
        payload["creditCardHolderInfo"] = credit_card_holder_info
    resp = requests.post(
        f"{_base_url()}/payments",
        json=payload,
        headers=_headers(),
        timeout=30,
    )
    _raise_for_status(resp)
    return resp.json()


def get_pix_qr_code(payment_id: str) -> dict:
    """Retorna o QR code PIX (encodedImage em base64 e payload copia-e-cola)."""
    resp = requests.get(
        f"{_base_url()}/payments/{payment_id}/pixQrCode",
        headers=_headers(),
        timeout=10,
    )
    _raise_for_status(resp)
    return resp.json()


def get_boleto_identification(payment_id: str) -> dict:
    """Retorna a linha digitável e nossoNumero de um boleto."""
    resp = requests.get(
        f"{_base_url()}/payments/{payment_id}/identificationField",
        headers=_headers(),
        timeout=10,
    )
    _raise_for_status(resp)
    return resp.json()


def get_payment(payment_id: str) -> dict:
    """Retorna os detalhes de uma cobrança pelo ID."""
    resp = requests.get(
        f"{_base_url()}/payments/{payment_id}",
        headers=_headers(),
        timeout=10,
    )
    _raise_for_status(resp)
    return resp.json()


def is_payment_confirmed(payment_id: str) -> bool:
    """Retorna True se a cobrança foi paga/confirmada."""
    try:
        payment = get_payment(payment_id)
        return payment.get("status") in CONFIRMED_STATUSES
    except Exception:
        return False
