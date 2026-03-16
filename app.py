"""
Aplicação Flask para análise de exames médicos com IA (Gemini).
Permite upload de uma ou múltiplas imagens de exames e gera laudos comparativos.
"""

import os
import sys
import uuid
import base64
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
MAX_IMAGES_PER_ANALYSIS = 5  # máximo de imagens por análise

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


def save_upload_file(file) -> tuple[Path, str, str] | None:
    """
    Salva um arquivo de upload em disco.
    Retorna (filepath, image_b64, image_mime) ou None se inválido.
    """
    if not file or file.filename == "" or not allowed_file(file.filename):
        return None

    original_name = secure_filename(file.filename)
    unique_name = f"{uuid.uuid4().hex}_{original_name}"
    filepath = UPLOAD_FOLDER / unique_name
    file.save(str(filepath))

    ext = filepath.suffix.lower().lstrip(".")
    mime_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "webp": "image/webp", "gif": "image/gif",
    }
    image_mime = mime_map.get(ext, "image/jpeg")

    with open(str(filepath), "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    return filepath, image_b64, image_mime


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    """Endpoint para receber e analisar o(s) exame(s) médico(s)."""
    api_key = get_api_key()
    if not api_key:
        flash("Erro: GEMINI_API_KEY não configurada. Adicione a variável de ambiente no painel do Vercel (Settings → Environment Variables).", "error")
        return redirect(url_for("index"))

    # Suporta múltiplos arquivos via campo "exam_images[]" ou "exam_images"
    files = request.files.getlist("exam_images") or request.files.getlist("exam_images[]")

    # Compatibilidade com campo legado "exam_image" (singular)
    if not files or all(f.filename == "" for f in files):
        legacy = request.files.get("exam_image")
        if legacy and legacy.filename != "":
            files = [legacy]
        else:
            flash("Nenhuma imagem enviada.", "error")
            return redirect(url_for("index"))

    # Limita ao máximo de imagens permitidas
    files = [f for f in files if f.filename != ""][:MAX_IMAGES_PER_ANALYSIS]

    if not files:
        flash("Nenhuma imagem válida enviada.", "error")
        return redirect(url_for("index"))

    user_description = request.form.get("description", "").strip()

    filepaths = []
    images_data = []  # Lista de {"b64": str, "mime": str} para exibição no resultado

    for file in files:
        result = save_upload_file(file)
        if result is None:
            continue
        filepath, image_b64, image_mime = result
        filepaths.append(str(filepath))
        images_data.append({"b64": image_b64, "mime": image_mime})

    if not filepaths:
        flash(f"Formato de arquivo não suportado. Use: {', '.join(ALLOWED_EXTENSIONS)}", "error")
        return redirect(url_for("index"))

    try:
        result = analyze_exam(
            exam_image_paths=filepaths,
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
            images=images_data,
            num_images=result["num_images"],
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
        for fp in filepaths:
            try:
                Path(fp).unlink()
            except Exception:
                pass


@app.route("/trial")
def trial():
    """Página de teste gratuito para embed via iframe."""
    return render_template("trial.html")


@app.route("/trial/analyze", methods=["POST"])
def trial_analyze():
    """Endpoint para análise no modo de teste gratuito (retorna JSON, aceita apenas 1 imagem)."""
    api_key = get_api_key()
    if not api_key:
        return jsonify({"error": "Serviço temporariamente indisponível. Tente novamente mais tarde."}), 503

    if "exam_image" not in request.files:
        return jsonify({"error": "Nenhuma imagem enviada."}), 400

    file = request.files["exam_image"]
    if file.filename == "":
        return jsonify({"error": "Nenhum arquivo selecionado."}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": f"Formato não suportado. Use: {', '.join(ALLOWED_EXTENSIONS)}"}), 400

    user_description = request.form.get("description", "").strip()

    result_save = save_upload_file(file)
    if result_save is None:
        return jsonify({"error": "Erro ao processar o arquivo enviado."}), 400

    filepath, image_b64, image_mime = result_save

    try:
        result = analyze_exam(
            exam_image_paths=[str(filepath)],
            api_key=api_key,
            user_description=user_description,
            model_name=get_model_name(),
        )
        return jsonify({
            "analysis": result["analysis"],
            "exam_type": result["exam_type"].replace("_", " ").title(),
            "references_used": result["references_used"],
            "model_used": result["model_used"],
            "image_b64": image_b64,
            "image_mime": image_mime,
        }), 200

    except Exception as e:
        error_msg = str(e)
        print(f"[ERRO TRIAL ANÁLISE] {type(e).__name__}: {error_msg}", file=sys.stderr)
        error_lower = error_msg.lower()
        if "api key not valid" in error_lower or "invalid api key" in error_lower:
            return jsonify({"error": "Serviço temporariamente indisponível."}), 503
        elif "quota" in error_lower or "rate limit" in error_lower or "resource_exhausted" in error_lower:
            return jsonify({"error": "Muitas solicitações. Tente novamente em alguns instantes."}), 429
        return jsonify({"error": f"Erro durante a análise: {error_msg}"}), 500

    finally:
        try:
            filepath.unlink()
        except Exception:
            pass


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    """Endpoint REST para integração programática. Suporta múltiplas imagens."""
    api_key = request.headers.get("X-API-Key") or get_api_key()
    if not api_key:
        return jsonify({"error": "API key não fornecida"}), 401

    # Suporta múltiplos arquivos via exam_images ou exam_images[]
    files = request.files.getlist("exam_images") or request.files.getlist("exam_images[]")
    if not files or all(f.filename == "" for f in files):
        # Compatibilidade com campo legado
        legacy = request.files.get("exam_image")
        if legacy:
            files = [legacy]
        else:
            return jsonify({"error": "Nenhuma imagem enviada"}), 400

    files = [f for f in files if f and f.filename != "" and allowed_file(f.filename)]
    files = files[:MAX_IMAGES_PER_ANALYSIS]

    if not files:
        return jsonify({"error": "Formato de arquivo não suportado"}), 400

    user_description = request.form.get("description", "")

    filepaths = []
    for file in files:
        original_name = secure_filename(file.filename)
        unique_name = f"{uuid.uuid4().hex}_{original_name}"
        filepath = UPLOAD_FOLDER / unique_name
        file.save(str(filepath))
        filepaths.append(str(filepath))

    try:
        result = analyze_exam(
            exam_image_paths=filepaths,
            api_key=api_key,
            user_description=user_description,
            model_name=get_model_name(),
        )
        return jsonify(result), 200

    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

    finally:
        for fp in filepaths:
            try:
                Path(fp).unlink()
            except Exception:
                pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
