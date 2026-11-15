"""Leitura de configuração: valores de exemplo do .env contam como "não configurado"."""

import re

# O .env.example traz textos como "sua_chave_gemini_aqui" e
# "https://SEU-PROJETO.supabase.co". Copiado sem editar, o app tentava usá-los:
# a chamada ao Gemini falhava e o Supabase perdia segundos em erros de DNS.
_PLACEHOLDER = re.compile(r"^(sua|seu|your)[_-]|seu-projeto|your-project", re.IGNORECASE)


def is_placeholder(value: str | None) -> bool:
    return bool(value) and bool(_PLACEHOLDER.search(value.strip()))


def configured(value: str | None) -> str:
    """O valor sem espaços, ou "" quando vazio ou de exemplo."""
    value = (value or "").strip()
    return "" if is_placeholder(value) else value
