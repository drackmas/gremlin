"""Audio routes: TTS voices/stream and STT models/transcribe, plus knowledge sync."""

from __future__ import annotations

import base64
import logging

from flask import Blueprint, Response, jsonify, request

from config import AppConfig
from stt import DEFAULT_STT_MODEL, MODELS, STTEngine, STTModelError, model_by_id
from tts import DEFAULT_VOICE, MAX_TEXT_CHARS, PiperTTS, TTSModelError, VOICES, voice_by_id

log = logging.getLogger("gremlin")


def create_bp(tts: PiperTTS, stt: STTEngine, settings_loader, cfg: AppConfig) -> Blueprint:
    """Build and return the audio Blueprint."""
    bp = Blueprint("audio", __name__)

    # --- knowledge library (sync is ON-PURPOSE only; the index is disposable) -
    @bp.post("/api/knowledge/sync")
    def knowledge_sync():
        from knowledge.config import KnowledgeConfig
        from knowledge.sync import sync

        kcfg = KnowledgeConfig(
            root=cfg.knowledge_dir,
            source_dir=cfg.knowledge_source_dir,
            models_dir=cfg.models_dir,
        )
        try:
            report = sync(kcfg)
        except Exception as e:
            log.exception("knowledge sync failed")
            return jsonify({"error": f"sync failed: {e}"}), 500
        return jsonify({"report": report.details()})

    # --- TTS (Piper, local voices; selection via settings piper_voice) -----
    @bp.get("/api/tts/voices")
    def tts_voices():
        return jsonify(
            [
                {"id": v.id, "label": v.label, "available": tts.available(v.id)}
                for v in VOICES
            ]
        )

    @bp.post("/api/tts/stream")
    def tts_stream():
        s = settings_loader()
        if not s.get("piper_tts_enabled"):
            return jsonify({"error": "Piper TTS is disabled"}), 400
        data = request.get_json(silent=True) or {}
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify({"error": "text must not be empty"}), 400
        if len(text) > MAX_TEXT_CHARS:
            return jsonify(
                {"error": f"text must be at most {MAX_TEXT_CHARS} characters"}
            ), 400
        voice_id = (s.get("piper_voice") or DEFAULT_VOICE).strip()
        try:
            voice = voice_by_id(voice_id)
        except ValueError:
            return jsonify({"error": f"unknown voice: {voice_id}"}), 400
        if not tts.available(voice.id):
            missing = tts.describe_missing(voice.id)
            log.warning("Piper model not found: %s", missing)
            return jsonify({"error": f"Piper model not found: {missing}"}), 503
        try:
            sample_rate = tts.ensure_ready(voice.id)
        except TTSModelError as e:
            log.warning("TTS unavailable: %s", e)
            return jsonify({"error": str(e)}), 503

        def generate():
            for chunk in tts.synthesize_chunks(text, voice_id=voice.id):
                yield chunk

        resp = Response(generate(), content_type="application/octet-stream")
        resp.headers["X-Audio-Sample-Rate"] = str(sample_rate)
        resp.headers["X-Audio-Format"] = "s16le"
        resp.headers["X-Audio-Channels"] = "1"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    # --- STT (local models; engine/model selection via settings) ------------
    @bp.get("/api/stt/models")
    def stt_models():
        return jsonify(
            [
                {
                    "id": m.id,
                    "label": m.label,
                    "engine": m.engine,
                    "approx_mb": m.approx_mb,
                    "available": stt.available(m.id),
                }
                for m in MODELS
            ]
        )

    @bp.post("/api/stt/transcribe")
    def stt_transcribe():
        s = settings_loader()
        if not s.get("stt_enabled"):
            return jsonify({"error": "speech-to-text is disabled"}), 400
        data = request.get_json(silent=True) or {}
        model_id = (data.get("model") or s.get("stt_model") or DEFAULT_STT_MODEL).strip()
        try:
            model = model_by_id(model_id)
        except ValueError:
            return jsonify({"error": f"unknown STT model: {model_id}"}), 400
        audio = data.get("audio") or ""
        if not isinstance(audio, str) or not audio:
            return jsonify({"error": "audio is required (base64 WAV)"}), 400
        try:
            raw = base64.b64decode(audio, validate=False)
        except (ValueError, TypeError):
            return jsonify({"error": "audio must be valid base64"}), 400
        if not raw:
            return jsonify({"error": "audio is empty"}), 400
        if not stt.available(model.id):
            missing = stt.describe_missing(model.id)
            log.warning("STT model not found: %s", missing)
            return jsonify({"error": f"STT model not found: {missing}"}), 503
        try:
            text = stt.transcribe(raw, model_id=model.id)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except STTModelError as e:
            log.warning("STT unavailable: %s", e)
            return jsonify({"error": str(e)}), 503
        return jsonify({"text": text})

    return bp
