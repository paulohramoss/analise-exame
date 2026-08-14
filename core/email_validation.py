"""
Validação de e-mail para criação de conta — Three Health Platform.

Três camadas, aplicadas nesta ordem:

1. **Sintaxe** — regex prático baseado na RFC 5322 mais os limites de tamanho
   da RFC 5321 (64 chars no local part, 254 no endereço todo).
2. **Domínio descartável** — blocklist de provedores temporários
   (mailinator, tempmail, 10minutemail…), extensível por env var.
3. **DNS/MX** — o domínio precisa publicar MX. Bloqueia domínios inexistentes,
   domínios parkeados que só têm registro A (`arroz.com`) e domínios com null
   MX (RFC 7505, caso do `example.com`).

A camada 3 é *fail-open*: se o DNS não responder (timeout, servfail, dnspython
ausente) o cadastro passa. Um problema transitório de rede nunca deve derrubar
um checkout pago — só rejeitamos quando a resposta do DNS é conclusiva.
"""

import logging
import os
import re
import time

logger = logging.getLogger(__name__)

# Limites da RFC 5321 §4.5.3.1
_MAX_LOCAL = 64
_MAX_TOTAL = 254

# Regex prático (RFC 5322 sem os casos exóticos de quoted-string/comentário):
# local part sem pontos consecutivos nem ponto nas pontas, domínio com labels
# válidos e TLD alfabético de 2+ letras.
_RE_EMAIL = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}$"
)

# Provedores de e-mail temporário/descartável mais usados para burlar cadastro.
DOMINIOS_DESCARTAVEIS = frozenset({
    "0-mail.com", "10minutemail.com", "10minutemail.net", "20minutemail.com",
    "33mail.com", "throwawaymail.com", "trashmail.com", "trashmail.de",
    "trash-mail.com", "tempmail.com", "tempmail.net", "temp-mail.org",
    "temp-mail.io", "tempmailo.com", "tempr.email", "tmpmail.org",
    "tmpmail.net", "mailinator.com", "mailinator.net", "mailinator2.com",
    "sogetthis.com", "guerrillamail.com", "guerrillamail.net",
    "guerrillamail.org", "guerrillamail.biz", "guerrillamail.de",
    "sharklasers.com", "grr.la", "spam4.me", "yopmail.com", "yopmail.fr",
    "yopmail.net", "cool.fr.nf", "jetable.org", "nospam.ze.tc",
    "getnada.com", "nada.email", "dispostable.com", "maildrop.cc",
    "mailnesia.com", "mintemail.com", "mytemp.email", "fakeinbox.com",
    "fakemailgenerator.com", "emailondeck.com", "e4ward.com",
    "spamgourmet.com", "mailcatch.com", "moakt.com", "mohmal.com",
    "inboxkitten.com", "linshiyouxiang.net", "burnermail.io",
    "anonaddy.me", "simplelogin.com", "harakirimail.com", "vomoto.com",
    "airmail.cc", "cock.li", "byom.de", "einrot.com", "1secmail.com",
    "1secmail.net", "1secmail.org", "wwjmp.com", "dropmail.me",
    "minuteinbox.com", "mail-temporaire.fr", "emailtemporario.com.br",
    "descartavel.com.br", "tempmail.plus", "mailtemp.top", "disbox.net",
    "edu.pl.ua",
})

_ERRO_FORMATO = "Informe um e-mail válido (ex.: nome@clinica.com.br)."
_ERRO_DESCARTAVEL = "E-mails temporários/descartáveis não são aceitos. Use seu e-mail profissional."
_ERRO_DOMINIO = "O domínio deste e-mail não existe ou não recebe mensagens. Confira se digitou corretamente."

# Cache de resultados conclusivos do DNS. Resultados indeterminados nunca são
# cacheados — a próxima tentativa deve poder consultar de novo.
_mx_cache: dict[str, bool] = {}
_mx_cache_expiracao: dict[str, float] = {}
_MX_CACHE_TTL_SECONDS = 3600


def _dominios_bloqueados() -> frozenset[str]:
    """Blocklist final: a lista embutida mais EMAIL_DOMINIOS_BLOQUEADOS (CSV)."""
    extras = os.environ.get("EMAIL_DOMINIOS_BLOQUEADOS", "")
    if not extras.strip():
        return DOMINIOS_DESCARTAVEIS
    adicionais = {d.strip().lower().lstrip("@") for d in extras.split(",") if d.strip()}
    return DOMINIOS_DESCARTAVEIS | adicionais


def normalizar_email(email: str) -> str:
    """Forma canônica usada em todo o app: sem espaços nas pontas e minúsculo."""
    return (email or "").strip().lower()


def dominio_aceita_email(dominio: str, timeout: float | None = None) -> bool | None:
    """
    Consulta DNS do domínio.

    True  = publica MX válido.
    False = domínio não existe, não publica MX, ou declara null MX (RFC 7505).
    None  = indeterminado (timeout, servfail, dnspython indisponível).
    """
    dominio = dominio.strip().lower().rstrip(".")
    if not dominio:
        return False

    agora = time.time()
    if dominio in _mx_cache and _mx_cache_expiracao.get(dominio, 0) > agora:
        return _mx_cache[dominio]

    resultado = _consultar_dns(dominio, timeout)
    if resultado is not None:
        _mx_cache[dominio] = resultado
        _mx_cache_expiracao[dominio] = agora + _MX_CACHE_TTL_SECONDS
    return resultado


def _consultar_dns(dominio: str, timeout: float | None) -> bool | None:
    try:
        import dns.resolver  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("dnspython não instalado — checagem de MX desativada.")
        return None

    if timeout is None:
        try:
            timeout = float(os.environ.get("EMAIL_MX_TIMEOUT", "3"))
        except ValueError:
            timeout = 3.0

    resolver = dns.resolver.Resolver()
    resolver.timeout = timeout
    resolver.lifetime = timeout

    try:
        respostas = resolver.resolve(dominio, "MX")
        # Null MX (RFC 7505): um único registro com exchange "." significa
        # explicitamente "este domínio não recebe e-mail".
        trocas = [str(r.exchange).rstrip(".") for r in respostas]
        if trocas and all(not t for t in trocas):
            return False
        return bool(trocas)
    except dns.resolver.NXDOMAIN:
        return False
    except dns.resolver.NoAnswer:
        pass  # domínio existe mas não publica MX — ver MX implícito abaixo
    except Exception:
        logger.info("Consulta MX indeterminada para %s", dominio, exc_info=True)
        return None

    # Domínio sem MX. A RFC 5321 §5.1 permite entregar no registro A ("MX
    # implícito"), mas na prática todo domínio que recebe e-mail publica MX —
    # quem só tem A costuma ser domínio parkeado (arroz.com é assim). Por isso
    # rejeitamos por padrão; EMAIL_MX_IMPLICITO=1 volta ao comportamento da RFC.
    if os.environ.get("EMAIL_MX_IMPLICITO", "0").strip().lower() not in ("1", "true", "yes"):
        return False

    for tipo in ("A", "AAAA"):
        try:
            if resolver.resolve(dominio, tipo):
                return True
        except dns.resolver.NoAnswer:
            continue
        except dns.resolver.NXDOMAIN:
            return False
        except Exception:
            logger.info("Consulta %s indeterminada para %s", tipo, dominio, exc_info=True)
            return None

    return False


def validar_email_cadastro(email: str, verificar_dns: bool | None = None) -> tuple[str, str]:
    """
    Valida um e-mail de cadastro.

    Retorna `(email_normalizado, erro)`. `erro` vazio significa válido — e o
    e-mail normalizado é o que deve ser persistido.

    `verificar_dns=None` (padrão) segue a env var EMAIL_MX_CHECK, ligada por
    padrão; passe False para pular a consulta de rede.
    """
    normalizado = normalizar_email(email)

    if not normalizado or len(normalizado) > _MAX_TOTAL:
        return "", _ERRO_FORMATO

    if not _RE_EMAIL.match(normalizado):
        return "", _ERRO_FORMATO

    local, _, dominio = normalizado.rpartition("@")
    if len(local) > _MAX_LOCAL:
        return "", _ERRO_FORMATO

    if dominio in _dominios_bloqueados():
        return "", _ERRO_DESCARTAVEL

    if verificar_dns is None:
        verificar_dns = os.environ.get("EMAIL_MX_CHECK", "1").strip().lower() not in ("0", "false", "no")

    if verificar_dns and dominio_aceita_email(dominio) is False:
        return "", _ERRO_DOMINIO

    return normalizado, ""
