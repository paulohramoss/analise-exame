"""
Resolve a versão do build (SHA curto do commit) para telemetria — usada como
`release` no Sentry (web e worker) e no rodapé da UI, para saber exatamente
qual deploy introduziu um erro.

Tenta `git rev-parse` primeiro (funciona em qualquer processo rodando a
partir do checkout do repo). Cai para as variáveis de ambiente que cada
plataforma de deploy expõe automaticamente quando não há diretório .git
disponível (ex.: alguns builds de container).
"""

import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Ordem de preferência: Vercel (processo web), Railway e Render (candidatos
# mais comuns para hospedar o worker, que precisa de processo always-on).
_DEPLOY_SHA_ENV_VARS = ("VERCEL_GIT_COMMIT_SHA", "RAILWAY_GIT_COMMIT_SHA", "RENDER_GIT_COMMIT")


def get_build_version() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            cwd=_REPO_ROOT,
        ).decode().strip()
    except Exception:
        pass
    for env_var in _DEPLOY_SHA_ENV_VARS:
        sha = os.environ.get(env_var, "").strip()
        if sha:
            return sha[:7]
    return "dev"
