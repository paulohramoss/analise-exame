"""
Minimização de dados (LGPD art. 6º, III) no texto clínico livre.

A descrição digitada pelo profissional segue para dois provedores de IA
(Google e Anthropic) e fica gravada no banco. O laudo não precisa saber QUEM é
o paciente — idade, sexo, queixa e evolução bastam. Aqui removemos o que
identifica a pessoa antes de qualquer envio ou gravação:

- CPF, CNPJ, CNS (cartão SUS), RG
- números de prontuário, atendimento, protocolo e matrícula
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
    (
        # Números de cadastro que laudos em PDF trazem no cabeçalho:
        # "Prontuário: 123456", "Atendimento nº 98.765", "Protocolo 2026/0001".
        "registro",
        re.compile(
            r"\b(?i:prontu[aá]rio|atendimento|protocolo|matr[ií]cula|carteir(?:a|inha)|"
            r"c[oó]digo\s+do\s+paciente|n[º°o]\.?\s*(?:do\s+)?(?:exame|pedido))"
            r"\s*(?:n[º°o]\.?)?\s*[:#\-–]?\s*\d[\d./-]{2,}"
        ),
    ),
    ("telefone", re.compile(r"(?:\+?55\s?)?\(?\b\d{2}\)?\s?9?\d{4}[\s-]?\d{4}\b")),
    (
        "nome",
        re.compile(
            # "Paciente: Maria da Silva", "Nome - João Souza", "Sra. Ana Lima" e, como
            # vem nos cabeçalhos de laudo em PDF, "PACIENTE: MARIA DA SILVA".
            # Exige palavras iniciadas em maiúscula: "paciente: 45 anos" fica intacto.
            r"\b(?i:paciente|nome(?:\s+completo)?(?:\s+do\s+paciente)?|sr\.?|sra\.?)\s*[:\-–]?\s*"
            r"(?P<nome>(?:[A-ZÀ-Ý][a-zà-ÿ]+|[A-ZÀ-Ý]{2,})"
            # Nome não atravessa linha (o cabeçalho do PDF segue na linha de baixo)
            # e tem no máximo 6 palavras.
            r"(?:[ \t]+(?:(?:d[aeo]s?|D[AEO]S?)[ \t]+)?(?:[A-ZÀ-Ý][a-zà-ÿ]+|[A-ZÀ-Ý]{2,})){1,5})",
        ),
    ),
]


def find_identifier_spans(text: str) -> list[tuple[int, int, str]]:
    """Posições (início, fim, tipo) de cada identificador no texto original.

    Usada para tarjar o identificador na imagem de uma página de PDF (ver
    web/uploads.py): o texto da página aponta cada caractere para sua posição.
    """
    if not text:
        return []
    spans = []
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span("nome") if kind == "nome" else match.span()
            spans.append((start, end, kind))
    return sorted(spans)


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
