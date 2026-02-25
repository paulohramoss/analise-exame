"""
Aplicação Flask para análise de exames médicos com IA (Gemini).
Permite upload de imagens de exames e gera laudos comparativos.
"""

import os
import sys
import uuid
from pathlib import Path
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

from core.analyzer import analyze_exam

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-in-prod")

UPLOAD_FOLDER = Path("/tmp/uploads")
UPLOAD_FOLDER.mkdir(exist_ok=True)
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif", "dcm"}
MAX_CONTENT_LENGTH = 20 * 1024 * 1024  # 20MB

app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_api_key() -> str:
    """Obtém a chave da API do Gemini."""
    return os.environ.get("GEMINI_API_KEY", "")


def get_model_name() -> str:
    """Obtém o modelo Gemini configurado."""
    return os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    """Endpoint para receber e analisar o exame médico."""
    api_key = get_api_key()
    if not api_key:
        flash("Erro: GEMINI_API_KEY não configurada. Adicione a variável de ambiente no painel do Vercel (Settings → Environment Variables).", "error")
        return redirect(url_for("index"))

    if "exam_image" not in request.files:
        flash("Nenhuma imagem enviada.", "error")
        return redirect(url_for("index"))

    file = request.files["exam_image"]
    if file.filename == "":
        flash("Nenhum arquivo selecionado.", "error")
        return redirect(url_for("index"))

    if not allowed_file(file.filename):
        flash(f"Formato de arquivo não suportado. Use: {', '.join(ALLOWED_EXTENSIONS)}", "error")
        return redirect(url_for("index"))

    user_description = request.form.get("description", "").strip()

    # Salva o arquivo com nome único
    original_name = secure_filename(file.filename)
    unique_name = f"{uuid.uuid4().hex}_{original_name}"
    filepath = UPLOAD_FOLDER / unique_name
    file.save(str(filepath))

    try:
        result = analyze_exam(
            exam_image_path=str(filepath),
            api_key=api_key,
            user_description=user_description,
            model_name=get_model_name(),
        )

        return render_template(
            "result.html",
            analysis=result["analysis"],
            exam_type=result["exam_type"].replace("_", " ").title(),
            references_used=result["references_used"],
            model_used=result["model_used"],
            image_filename=unique_name,
            user_description=user_description,
        )

    except Exception as e:
        error_msg = str(e)
        print(f"[ERRO ANÁLISE] {type(e).__name__}: {error_msg}", file=sys.stderr)
        error_lower = error_msg.lower()
        if "api key not valid" in error_lower or "invalid api key" in error_lower or "api_key_invalid" in error_lower:
            flash("Erro de autenticação: GEMINI_API_KEY inválida. Verifique a chave no painel do Vercel.", "error")
        elif "quota" in error_lower or "rate limit" in error_lower or "resource_exhausted" in error_lower:
            flash("Cota da API excedida. Tente novamente mais tarde.", "error")
        else:
            flash(f"Erro durante a análise: {error_msg}", "error")
        return redirect(url_for("index"))

    finally:
        # Remove arquivo temporário após análise
        try:
            filepath.unlink()
        except Exception:
            pass


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    """Endpoint REST para integração programática."""
    api_key = request.headers.get("X-API-Key") or get_api_key()
    if not api_key:
        return jsonify({"error": "API key não fornecida"}), 401

    if "exam_image" not in request.files:
        return jsonify({"error": "Nenhuma imagem enviada"}), 400

    file = request.files["exam_image"]
    if not allowed_file(file.filename):
        return jsonify({"error": "Formato de arquivo não suportado"}), 400

    user_description = request.form.get("description", "")

    original_name = secure_filename(file.filename)
    unique_name = f"{uuid.uuid4().hex}_{original_name}"
    filepath = UPLOAD_FOLDER / unique_name
    file.save(str(filepath))

    try:
        result = analyze_exam(
            exam_image_path=str(filepath),
            api_key=api_key,
            user_description=user_description,
            model_name=get_model_name(),
        )
        return jsonify(result), 200

    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

    finally:
        try:
            filepath.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
