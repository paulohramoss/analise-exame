"""Testes da validação de e-mail no cadastro (core/email_validation.py)."""

import pytest

from core import email_validation as ev


@pytest.fixture(autouse=True)
def _limpa_cache_mx():
    ev._mx_cache.clear()
    ev._mx_cache_expiracao.clear()
    yield
    ev._mx_cache.clear()
    ev._mx_cache_expiracao.clear()


# ── Sintaxe ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("email", [
    "fulano@clinica.com.br",
    "dr.silva+exames@gmail.com",
    "a_b-c'd@sub.dominio.co",
    "NOME@Clinica.COM",
])
def test_aceita_emails_bem_formados(email):
    normalizado, erro = ev.validar_email_cadastro(email, verificar_dns=False)
    assert erro == ""
    assert normalizado == email.strip().lower()


@pytest.mark.parametrize("email", [
    "",
    "   ",
    "sem-arroba.com",
    "@dominio.com",
    "fulano@",
    "fulano@dominio",          # sem TLD
    "fulano@dominio..com",     # ponto duplo no domínio
    "fulano..silva@a.com",     # ponto duplo no local part
    ".fulano@a.com",           # ponto no início
    "fulano.@a.com",           # ponto no fim
    "fulano@-dominio.com",     # label começando com hífen
    "fulano@dominio.c",        # TLD de 1 char
    "fulano@dominio.123",      # TLD numérico
    "fulano silva@a.com",      # espaço
    "fulano@a.com extra",
    "fulano@@a.com",
    "fulanão@a.com",           # unicode no local part
])
def test_rejeita_emails_malformados(email):
    normalizado, erro = ev.validar_email_cadastro(email, verificar_dns=False)
    assert erro != ""
    assert normalizado == ""


def test_rejeita_local_part_acima_de_64_chars():
    email = "a" * 65 + "@clinica.com.br"
    _, erro = ev.validar_email_cadastro(email, verificar_dns=False)
    assert erro != ""


def test_rejeita_endereco_acima_de_254_chars():
    email = "a" * 60 + "@" + ("b" * 60 + ".") * 3 + "com" * 20
    _, erro = ev.validar_email_cadastro(email, verificar_dns=False)
    assert erro != ""


# ── Descartáveis ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("email", [
    "fulano@mailinator.com",
    "FULANO@YOPMAIL.COM",
    "x@10minutemail.com",
    "x@temp-mail.org",
])
def test_rejeita_dominio_descartavel(email):
    _, erro = ev.validar_email_cadastro(email, verificar_dns=False)
    assert "descartáveis" in erro


def test_dominio_extra_bloqueado_por_env(monkeypatch):
    monkeypatch.setenv("EMAIL_DOMINIOS_BLOQUEADOS", "arroz.com, @naoquero.com")

    _, erro = ev.validar_email_cadastro("arroz@arroz.com", verificar_dns=False)
    assert "descartáveis" in erro

    _, erro = ev.validar_email_cadastro("x@naoquero.com", verificar_dns=False)
    assert "descartáveis" in erro

    _, erro = ev.validar_email_cadastro("x@clinica.com.br", verificar_dns=False)
    assert erro == ""


# ── DNS ───────────────────────────────────────────────────────────────────────

def test_rejeita_dominio_sem_mx(monkeypatch):
    monkeypatch.setattr(ev, "dominio_aceita_email", lambda dominio, timeout=None: False)
    _, erro = ev.validar_email_cadastro("arroz@arroz.com", verificar_dns=True)
    assert "domínio" in erro


def test_aceita_dominio_com_mx(monkeypatch):
    monkeypatch.setattr(ev, "dominio_aceita_email", lambda dominio, timeout=None: True)
    normalizado, erro = ev.validar_email_cadastro("fulano@clinica.com.br", verificar_dns=True)
    assert erro == ""
    assert normalizado == "fulano@clinica.com.br"


def test_dns_indeterminado_nao_bloqueia_cadastro(monkeypatch):
    """Fail-open: timeout de DNS não pode derrubar um checkout pago."""
    monkeypatch.setattr(ev, "dominio_aceita_email", lambda dominio, timeout=None: None)
    _, erro = ev.validar_email_cadastro("fulano@clinica.com.br", verificar_dns=True)
    assert erro == ""


def test_env_desliga_checagem_de_dns(monkeypatch):
    def _explode(dominio, timeout=None):
        raise AssertionError("DNS não deveria ser consultado com EMAIL_MX_CHECK=0")

    monkeypatch.setattr(ev, "dominio_aceita_email", _explode)
    monkeypatch.setenv("EMAIL_MX_CHECK", "0")
    _, erro = ev.validar_email_cadastro("fulano@clinica.com.br")
    assert erro == ""


def test_resultado_conclusivo_do_dns_e_cacheado(monkeypatch):
    chamadas = []

    def _consulta(dominio, timeout):
        chamadas.append(dominio)
        return True

    monkeypatch.setattr(ev, "_consultar_dns", _consulta)
    assert ev.dominio_aceita_email("clinica.com.br") is True
    assert ev.dominio_aceita_email("clinica.com.br") is True
    assert chamadas == ["clinica.com.br"]


def test_resultado_indeterminado_nao_e_cacheado(monkeypatch):
    chamadas = []

    def _consulta(dominio, timeout):
        chamadas.append(dominio)
        return None

    monkeypatch.setattr(ev, "_consultar_dns", _consulta)
    assert ev.dominio_aceita_email("clinica.com.br") is None
    assert ev.dominio_aceita_email("clinica.com.br") is None
    assert len(chamadas) == 2


def _stub_dns(monkeypatch, respostas):
    """Instala um resolver falso: respostas[(dominio, tipo)] = lista | Exception."""
    import dns.resolver

    class _FakeResolver:
        timeout = None
        lifetime = None

        def resolve(self, dominio, tipo):
            r = respostas.get((dominio, tipo), dns.resolver.NoAnswer())
            if isinstance(r, Exception):
                raise r
            return r

    monkeypatch.setattr(dns.resolver, "Resolver", _FakeResolver)


class _MX:
    def __init__(self, exchange):
        self.exchange = exchange


def test_dominio_com_mx_valido(monkeypatch):
    _stub_dns(monkeypatch, {("clinica.com.br", "MX"): [_MX("mx1.google.com.")]})
    assert ev.dominio_aceita_email("clinica.com.br") is True


def test_null_mx_e_rejeitado(monkeypatch):
    """RFC 7505: exchange '.' significa 'este domínio não recebe e-mail'."""
    _stub_dns(monkeypatch, {("example.com", "MX"): [_MX(".")]})
    assert ev.dominio_aceita_email("example.com") is False


def test_nxdomain_e_rejeitado(monkeypatch):
    import dns.resolver
    _stub_dns(monkeypatch, {("naoexiste-12345.com.br", "MX"): dns.resolver.NXDOMAIN()})
    assert ev.dominio_aceita_email("naoexiste-12345.com.br") is False


def test_dominio_parkeado_so_com_registro_a_e_rejeitado(monkeypatch):
    """Caso arroz.com: sem MX, só A. Por padrão não aceitamos MX implícito."""
    _stub_dns(monkeypatch, {("arroz.com", "A"): ["1.2.3.4"]})
    assert ev.dominio_aceita_email("arroz.com") is False


def test_mx_implicito_pode_ser_reabilitado_por_env(monkeypatch):
    monkeypatch.setenv("EMAIL_MX_IMPLICITO", "1")
    _stub_dns(monkeypatch, {("arroz.com", "A"): ["1.2.3.4"]})
    assert ev.dominio_aceita_email("arroz.com") is True


def test_timeout_de_dns_e_indeterminado(monkeypatch):
    import dns.resolver
    _stub_dns(monkeypatch, {("clinica.com.br", "MX"): dns.resolver.LifetimeTimeout()})
    assert ev.dominio_aceita_email("clinica.com.br") is None


def test_dnspython_ausente_retorna_indeterminado(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _sem_dns(name, *args, **kwargs):
        if name.startswith("dns"):
            raise ImportError("sem dnspython")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _sem_dns)
    assert ev._consultar_dns("clinica.com.br", 1.0) is None
