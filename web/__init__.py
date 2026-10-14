"""
Camada web (Flask) da plataforma.

- config.py        ambiente, planos e chaves de IA
- security.py      cookies/tokens assinados, sessão premium, IP do cliente
- extensions.py    rate limiter
- accounts.py      plano ativo e emissão da sessão premium
- uploads.py       upload de imagens e conversão de DICOM
- costs.py         orçamento de custo diário e cota do trial
- presentation.py  formatação, consenso e mensagens para o usuário
- access_log.py    log de acesso assíncrono
- hooks.py         before/after request, CSRF, cabeçalhos, filtros Jinja
- routes/          Blueprints: analysis, laudos, billing, auth

A aplicação é montada em app.py (create_app).
"""
