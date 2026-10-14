"""
Minimização de dados (LGPD art. 6º, III) no texto clínico livre.

A descrição digitada pelo profissional segue para dois provedores de IA
(Google e Anthropic) e fica gravada no banco. O laudo não precisa saber QUEM é
o paciente — idade, sexo, queixa e evolução bastam. Aqui removemos o que
identifica a pessoa antes de qualquer envio ou gravação:

- CPF, CNPJ, CNS (cartão SUS), RG
- e-mail e telefone
- data de nascimento explícita ("nasc.", "DN", "data de nascimento")
- nome após rótulos como "paciente:", "nome:", "Sr./Sra."

É uma rede de segurança por padrões, não uma garantia: o aviso na tela de
upload continua pedindo para não digitar dados identificáveis.
"""

import re

_PLACEHOLDER = "[removido]"

# A ordem importa: padrões mais específicos (CNPJ, CPF, CNS) antes do telefone,
# que também casa sequências longas de dígitos.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("cnpj", re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")),
    ("cpf", re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")),
    ("cns", re.compile(r"\b[1-9]\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\b")),
    ("rg", re.compile(r"\bRG\s*:?\s*[\dA-Za-z.\-]{5,14}\b", re.IGNORECASE)),
    (
        "nascimento",
        re.compile(
            r"\b(?:data\s+de\s+nascimento|nascid[oa]\s+em|nasc\.?|DN)\s*:?\s*\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}",
            re.IGNORECASE,
        ),
    ),
    ("telefone", re.compile(r"(?:\+?55\s?)?\(?\b\d{2}\)?\s?9?\d{4}[\s-]?\d{4}\b")),
    (
        "nome",
        re.compile(
            # "Paciente: Maria da Silva", "Nome - João Souza", "Sra. Ana Lima".
            # Exige palavras iniciadas em maiúscula: "paciente: 45 anos" fica intacto.
            r"\b(?i:paciente|nome(?:\s+completo)?|sr\.?|sra\.?)\s*[:\-–]?\s*"
            r"(?P<nome>[A-ZÀ-Ý][a-zà-ÿ]+(?:\s+(?:d[aeo]s?\s+)?[A-ZÀ-Ý][a-zà-ÿ]+)+)",
        ),
    ),
]


def redact_identifiers(text: str) -> tuple[str, list[str]]:
    """Retorna (texto sem identificadores, tipos removidos)."""
    if not text:
        return text or "", []
    removed: list[str] = []
    for kind, pattern in _PATTERNS:
        if kind == "nome":
            def _replace_name(match: re.Match) -> str:
                start, end = match.span("nome")
                offset = match.start()
                whole = match.group(0)
                return whole[: start - offset] + _PLACEHOLDER + whole[end - offset:]
            text, count = pattern.subn(_replace_name, text)
        else:
            text, count = pattern.subn(_PLACEHOLDER, text)
        if count:
            removed.append(kind)
    return text, removed
