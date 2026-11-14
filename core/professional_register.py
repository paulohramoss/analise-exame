"""
Conferência do registro profissional (CRM, CREFITO e outros conselhos).

Não consulta os conselhos — eles não oferecem API pública. Confere o formato,
a UF/região e a coerência com o perfil escolhido, e devolve o registro numa
grafia única ("CRM-SP 123456", "CREFITO-3 123456-F"). Junto com o termo de
responsabilidade aceito, é o que dá para exigir sem burocracia: barra erro de
digitação e registro de outra profissão, não fraude deliberada.
"""

import re
import unicodedata

UFS = {
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA",
    "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO",
}
# CREFITO tem regiões numeradas (CREFITO-1 a CREFITO-20 hoje); folga para regiões novas.
_CREFITO_MAX_REGIAO = 25

PERFIL_MEDICO = "Médico"
PERFIL_FISIOTERAPEUTA = "Fisioterapeuta"
PERFIL_ESTUDANTE = "Estudante da área da saúde"
PERFIL_OUTRO = "Outro profissional de saúde"

# Perfis oferecidos nos formulários, na ordem de exibição.
PERFIS = (PERFIL_MEDICO, "Radiologista", "Ortopedista", "Laudista", PERFIL_FISIOTERAPEUTA,
          PERFIL_ESTUDANTE, PERFIL_OUTRO)
_PERFIS_CRM = {PERFIL_MEDICO, "Radiologista", "Ortopedista", "Laudista"}

EXEMPLO_CRM = "CRM-SP 123456"
EXEMPLO_CREFITO = "CREFITO-3 123456-F"

_CRM = re.compile(r"^CRM\s*[-/ ]?\s*([A-Z]{2})\s*[-/ ]?\s*(\d{3,7})$")
_CRM_NUMERO_PRIMEIRO = re.compile(r"^(?:CRM\s*)?(\d{3,7})\s*[-/ ]\s*([A-Z]{2})$")
_CREFITO = re.compile(r"^CREFITO\s*[-/ ]?\s*(\d{1,2})\s*[-/ ]?\s*(\d{3,7})\s*[-/ ]?\s*(F|TO|LTT)?$")
_OUTRO = re.compile(r"^([A-Z]{2,8})\s*[-/ ]?\s*([A-Z]{2}|\d{1,2})?\s*[-/ ]?\s*(\d{3,8})\s*[-/ ]?\s*([A-Z]{1,3})?$")


def _clean(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    plain = re.sub(r"\b(N[ºO°]|NUMERO|NUM)\b\.?", " ", plain.upper())
    return " ".join(plain.replace(".", "").split())


def _crm(texto: str) -> str | None:
    match = _CRM.match(texto)
    if match:
        uf, numero = match.groups()
    else:
        match = _CRM_NUMERO_PRIMEIRO.match(texto)
        if not match:
            return None
        numero, uf = match.groups()
    return f"CRM-{uf} {int(numero)}" if uf in UFS else None


def _crefito(texto: str, sufixo_padrao: str | None = None) -> str | None:
    match = _CREFITO.match(texto)
    if not match:
        return None
    regiao, numero, sufixo = match.groups()
    if not 1 <= int(regiao) <= _CREFITO_MAX_REGIAO:
        return None
    sufixo = sufixo or sufixo_padrao
    return f"CREFITO-{int(regiao)} {int(numero)}" + (f"-{sufixo}" if sufixo else "")


def _outro(texto: str) -> str | None:
    match = _OUTRO.match(texto)
    if not match:
        return None
    sigla, regiao, numero, sufixo = match.groups()
    if regiao and regiao.isalpha() and regiao not in UFS:
        return None
    return sigla + (f"-{regiao}" if regiao else "") + f" {int(numero)}" + (f"-{sufixo}" if sufixo else "")


def registro_obrigatorio(perfil: str) -> bool:
    """Perfis que precisam de registro para assinar como responsável."""
    return perfil in _PERFIS_CRM or perfil in {PERFIL_FISIOTERAPEUTA, PERFIL_OUTRO}


def validar_registro(perfil: str, registro: str) -> tuple[str, str | None]:
    """(registro normalizado, mensagem de erro ou None).

    Sem perfil e sem registro não há o que conferir. Estudante não tem registro
    em conselho: o campo é ignorado.
    """
    perfil = (perfil or "").strip()
    texto = _clean(registro)

    if perfil == PERFIL_ESTUDANTE:
        return "", None
    if not texto:
        if registro_obrigatorio(perfil):
            exemplo = EXEMPLO_CREFITO if perfil == PERFIL_FISIOTERAPEUTA else EXEMPLO_CRM
            return "", f"Informe o registro profissional do perfil {perfil} (ex.: {exemplo})."
        return "", None

    if perfil in _PERFIS_CRM:
        normalizado = _crm(texto)
        if normalizado:
            return normalizado, None
        if _crefito(texto):
            return "", f"O perfil {perfil} usa registro no CRM; CREFITO é do perfil Fisioterapeuta."
        return "", f"Registro do CRM fora do formato esperado (ex.: {EXEMPLO_CRM}, com a UF do conselho)."

    if perfil == PERFIL_FISIOTERAPEUTA:
        normalizado = _crefito(texto, sufixo_padrao="F")
        if normalizado and normalizado.endswith("-F"):
            return normalizado, None
        if normalizado:
            return "", "O sufixo -TO/-LTT é de terapia ocupacional; o registro de fisioterapeuta termina em -F."
        if _crm(texto):
            return "", "O perfil Fisioterapeuta usa registro no CREFITO; CRM é do perfil Médico."
        return "", f"Registro do CREFITO fora do formato esperado (ex.: {EXEMPLO_CREFITO})."

    # Outro profissional ou perfil não informado: aceita CRM, CREFITO ou outro conselho
    # (COREN, CREF, CRN…) no formato SIGLA-UF NÚMERO.
    normalizado = _crm(texto) or _crefito(texto) or _outro(texto)
    if normalizado:
        return normalizado, None
    return "", "Registro profissional fora do formato esperado (ex.: COREN-SP 123456)."
