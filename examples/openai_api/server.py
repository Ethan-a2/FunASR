"""
FunASR OpenAI-Compatible API Server

Drop-in replacement for OpenAI's /v1/audio/transcriptions endpoint.
Works with any agent framework that supports OpenAI audio API.

Usage:
    python server.py --model sensevoice --device cuda --port 8000
    python server.py --model moss-transcribe-diarize --device cuda:0 --port 8000

Then use with any OpenAI-compatible client:
    curl http://localhost:8000/v1/audio/transcriptions \
      -F file=@audio.wav -F model=sensevoice
"""

import argparse
import base64
import binascii
import tempfile
import time
import os
import re
import logging
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="FunASR OpenAI-Compatible API", version="1.0.0")

MODEL_REGISTRY = {}
DEVICE = "cpu"
DEFAULT_MODEL = "sensevoice"
N8N_OPENAI_MODEL_ALIAS = "whisper-1"

MODEL_CONFIGS = {
    "sensevoice": {
        "model": "iic/SenseVoiceSmall",
        "vad_model": "fsmn-vad",
        "vad_kwargs": {"max_single_segment_time": 30000},
    },
    "paraformer": {
        "model": "paraformer-zh",
        "vad_model": "fsmn-vad",
        "punc_model": "ct-punc",
    },
    "paraformer-en": {
        "model": "paraformer-en",
        "vad_model": "fsmn-vad",
    },
    "fun-asr-nano": {
        "model": "FunAudioLLM/Fun-ASR-Nano-2512",
        "hub": "hf",
        "trust_remote_code": True,
        "vad_model": "fsmn-vad",
        "vad_kwargs": {"max_single_segment_time": 30000},
    },
    "moss-transcribe-diarize": {
        "model": "OpenMOSS-Team/MOSS-Transcribe-Diarize",
        "model_revision": "e8681d68e7042738ffca8ac8212bc8fcb1131ab8",
        "hub": "hf",
        "backend": "hf",
        "trust_remote_code": True,
    },
}

BASE64_AUDIO_FIELDS = ("file", "audio_base64", "audio")
AUDIO_EXTENSIONS_BY_MIME = {
    "audio/flac": ".flac",
    "audio/mp3": ".mp3",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
    "audio/x-m4a": ".m4a",
    "audio/x-wav": ".wav",
}

TRANSCRIPTION_OPENAPI_EXTRA = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                        "model": {"type": "string", "default": "sensevoice"},
                        "language": {"type": "string"},
                        "response_format": {
                            "type": "string",
                            "default": "json",
                            "enum": ["json", "verbose_json"],
                        },
                    },
                }
            },
            "application/json": {
                "schema": {
                    "type": "object",
                    "description": "Send Base64 audio in file, audio_base64, or audio. A data: URI is also accepted.",
                    "anyOf": [
                        {"required": ["file"]},
                        {"required": ["audio_base64"]},
                        {"required": ["audio"]},
                    ],
                    "properties": {
                        "file": {"type": "string", "description": "Base64 audio or a data: URI"},
                        "audio_base64": {"type": "string", "description": "Base64 audio or a data: URI"},
                        "audio": {"type": "string", "description": "Base64 audio or a data: URI"},
                        "filename": {"type": "string", "default": "audio.wav"},
                        "model": {"type": "string", "default": "sensevoice"},
                        "language": {"type": "string"},
                        "response_format": {
                            "type": "string",
                            "default": "json",
                            "enum": ["json", "verbose_json"],
                        },
                    },
                }
            },
        },
    }
}


def load_model(model_name: str):
    """Load a model and store in registry."""
    if model_name in MODEL_REGISTRY:
        return MODEL_REGISTRY[model_name]

    if model_name not in MODEL_CONFIGS:
        available = list(MODEL_CONFIGS.keys())
        raise ValueError(f"Unknown model '{model_name}'. Available: {available}")

    from funasr import AutoModel

    cfg = MODEL_CONFIGS[model_name].copy()
    cfg["device"] = DEVICE
    cfg["disable_update"] = True

    logger.info(f"Loading model '{model_name}' on {DEVICE}...")
    t0 = time.time()
    model = AutoModel(**cfg)
    elapsed = time.time() - t0
    logger.info(f"Model '{model_name}' loaded in {elapsed:.1f}s")

    MODEL_REGISTRY[model_name] = model
    return model


def clean_text(text: str) -> str:
    """Remove SenseVoice special tags from output."""
    return re.sub(r'<\|[^|]*\|>', '', text).strip()


def resolve_openai_transcription_model(requested_model: str) -> str:
    """Map n8n's fixed OpenAI transcription model to the started model."""
    if requested_model == N8N_OPENAI_MODEL_ALIAS:
        return DEFAULT_MODEL
    return requested_model


def _json_string(payload: dict, field_name: str, default: Optional[str] = None) -> Optional[str]:
    value = payload.get(field_name, default)
    if value is not None and not isinstance(value, str):
        raise HTTPException(status_code=422, detail=f"JSON field '{field_name}' must be a string")
    return value


def _decode_base64_audio(payload: dict) -> tuple[bytes, str]:
    encoded_audio = None
    for field_name in BASE64_AUDIO_FIELDS:
        if field_name in payload:
            encoded_audio = payload[field_name]
            break

    if not isinstance(encoded_audio, str) or not encoded_audio.strip():
        fields = ", ".join(BASE64_AUDIO_FIELDS)
        raise HTTPException(status_code=422, detail=f"JSON must include a non-empty Base64 field: {fields}")

    encoded_audio = encoded_audio.strip()
    mime_type = None
    data_uri_match = re.fullmatch(r"data:([^;,]+)?;base64,(.*)", encoded_audio, flags=re.IGNORECASE | re.DOTALL)
    if data_uri_match:
        mime_type = (data_uri_match.group(1) or "").lower()
        encoded_audio = data_uri_match.group(2)

    encoded_audio = re.sub(r"\s+", "", encoded_audio)
    encoded_audio += "=" * (-len(encoded_audio) % 4)
    try:
        content = base64.b64decode(encoded_audio, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="JSON audio field is not valid Base64") from exc

    if not content:
        raise HTTPException(status_code=422, detail="JSON audio field decodes to an empty file")

    filename = _json_string(payload, "filename") or _json_string(payload, "file_name")
    if filename:
        filename = os.path.basename(filename)
    if not filename or not os.path.splitext(filename)[1]:
        filename = f"audio{AUDIO_EXTENSIONS_BY_MIME.get(mime_type, '.wav')}"
    return content, filename


async def _parse_transcription_request(request: Request) -> tuple[bytes, str, str, Optional[str], str]:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    if media_type == "multipart/form-data":
        form = await request.form()
        uploaded_file = form.get("file")
        if uploaded_file is None or not callable(getattr(uploaded_file, "read", None)):
            raise HTTPException(status_code=422, detail="Multipart request must include a file field")

        content = await uploaded_file.read()
        if not content:
            raise HTTPException(status_code=422, detail="Uploaded audio file is empty")
        filename = os.path.basename(getattr(uploaded_file, "filename", None) or "audio.wav")
        model = form.get("model") if form.get("model") is not None else "sensevoice"
        language = form.get("language")
        response_format = form.get("response_format") if form.get("response_format") is not None else "json"
        return content, filename, model, language, response_format

    if media_type == "application/json" or media_type.endswith("+json"):
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Request body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="JSON request body must be an object")

        content, filename = _decode_base64_audio(payload)
        model = _json_string(payload, "model", "sensevoice") or "sensevoice"
        language = _json_string(payload, "language")
        response_format = _json_string(payload, "response_format", "json") or "json"
        return content, filename, model, language, response_format

    raise HTTPException(
        status_code=415,
        detail="Content-Type must be multipart/form-data or application/json",
    )


@app.post("/v1/audio/transcriptions", openapi_extra=TRANSCRIPTION_OPENAPI_EXTRA)
async def transcribe(request: Request):
    """
    OpenAI-compatible audio transcription endpoint.
    
    Accepts the same parameters as OpenAI's /v1/audio/transcriptions:
    - multipart/form-data: file upload in the file field
    - application/json: Base64 audio in file, audio_base64, or audio
    - model: Model to use (sensevoice, paraformer, fun-asr-nano, moss-transcribe-diarize)
    - language: Optional language hint
    - response_format: json or verbose_json
    """
    content, filename, model, language, response_format = await _parse_transcription_request(request)
    model = resolve_openai_transcription_model(model)

    # Validate model
    if model not in MODEL_CONFIGS:
        raise HTTPException(
            status_code=400,
            detail=f"Model '{model}' not found. Available: {list(MODEL_CONFIGS.keys())}"
        )

    # Save uploaded file
    suffix = os.path.splitext(filename)[1] or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        asr_model = load_model(model)
        t0 = time.time()

        generate_kwargs = {"input": tmp_path, "batch_size": 1}
        if language:
            generate_kwargs["language"] = language

        result = asr_model.generate(**generate_kwargs)
        elapsed = time.time() - t0

        text = clean_text(result[0]["text"])

        if response_format == "verbose_json":
            segments = []
            if "sentence_info" in result[0]:
                for seg in result[0]["sentence_info"]:
                    segments.append({
                        "start": seg.get("start", 0) / 1000.0,
                        "end": seg.get("end", 0) / 1000.0,
                        "text": clean_text(seg.get("text", "")),
                        "speaker": seg.get("spk", None),
                    })
            return JSONResponse({
                "text": text,
                "segments": segments,
                "language": language or "auto",
                "duration": round(elapsed, 3),
                "model": model,
            })
        else:
            return JSONResponse({"text": text})

    except Exception as e:
        logger.error(f"Transcription error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        os.unlink(tmp_path)


@app.get("/v1/models")
async def list_models():
    """List available models (OpenAI-compatible)."""
    models = []
    for name in MODEL_CONFIGS:
        models.append({
            "id": name,
            "object": "model",
            "created": 1700000000,
            "owned_by": "funasr",
            "ready": name in MODEL_REGISTRY,
        })
    return JSONResponse({"object": "list", "data": models})


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "ok",
        "device": DEVICE,
        "models_loaded": list(MODEL_REGISTRY.keys()),
        "models_available": list(MODEL_CONFIGS.keys()),
    }


def main():
    parser = argparse.ArgumentParser(description="FunASR OpenAI-Compatible API Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument("--device", default="cuda", help="Device: cuda, cpu, mps")
    parser.add_argument("--model", default="sensevoice", help="Pre-load model at startup")
    args = parser.parse_args()

    global DEFAULT_MODEL, DEVICE
    DEVICE = args.device
    DEFAULT_MODEL = args.model

    # Pre-load default model
    load_model(args.model)

    logger.info(f"FunASR API server starting on http://{args.host}:{args.port}")
    logger.info(f"  Device: {DEVICE}")
    logger.info(f"  Models: {list(MODEL_CONFIGS.keys())}")
    logger.info(f"  Docs:   http://{args.host}:{args.port}/docs")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
