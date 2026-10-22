"""Blueprints da aplicação (registrados em app.create_app)."""

from web.routes.account import bp as account_bp
from web.routes.analysis import bp as analysis_bp
from web.routes.auth import bp as auth_bp
from web.routes.billing import bp as billing_bp
from web.routes.laudos import bp as laudos_bp
from web.routes.pacientes import bp as pacientes_bp

ALL_BLUEPRINTS = (analysis_bp, laudos_bp, pacientes_bp, billing_bp, auth_bp, account_bp)
