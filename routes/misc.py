"""Misc routes: index page, model context, themes."""

from __future__ import annotations

from flask import Blueprint, jsonify, render_template

from config import AppConfig


def create_bp(
    cfg: AppConfig,
    settings_loader,
    model_context_fn,
    model_context_cache: dict,
) -> Blueprint:
    """Build and return the misc Blueprint."""
    bp = Blueprint("misc", __name__)

    @bp.get("/")
    def index():
        return render_template("index.html")

    @bp.get("/api/model/context")
    def model_context():
        s = settings_loader()
        base_url = (s.get("base_url") or "").rstrip("/")
        model = (s.get("model") or "").strip()
        return jsonify(model_context_fn(base_url, model, model_context_cache))

    @bp.get("/api/themes")
    def themes():
        out = ["default"]
        if cfg.themes_dir.is_dir():
            for p in sorted(cfg.themes_dir.glob("*.css")):
                slug = p.name[:-8] if p.name.endswith(".min.css") else p.stem
                out.append(slug)
        return jsonify(out)

    return bp
