# OpenAPI Spec for the FunASR OpenAI-Compatible API

(English|[简体中文](OPENAPI_zh.md))

Use [`openapi.json`](openapi.json) when you want to inspect, mock, document, or import the FunASR speech API before wiring it into an application, API gateway, workflow engine, or SDK generator.

The running FastAPI server also exposes live docs at `/docs` and a generated schema at `/openapi.json`. This checked-in spec is a portable reference for the example server in this directory.

## Import options

| Tool | How to use it |
|---|---|
| Swagger Editor or Redoc | Import `openapi.json` to inspect `/health`, `/v1/models`, and `/v1/audio/transcriptions`. |
| Postman | Import `openapi.json` if you prefer schema-driven collections, or use the ready-made [Postman collection](POSTMAN.md). |
| Dify, n8n, or internal workflow tools | Use the multipart or JSON Base64 request shape in the spec together with the [workflow recipes](WORKFLOWS.md). |
| API gateway or internal developer portal | Publish the spec and point the server URL to your reachable FunASR API endpoint. |
| Client generation | Generate a small internal client, then keep the multipart `file` field mapped to a binary upload. |

## Server URL

The spec includes local examples:

- `http://localhost:8000`
- `http://funasr-api:8000`

Replace them with the URL reachable from your app, container, or workflow runtime.

## Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | `GET` | Readiness check, selected device, loaded models, and available aliases. |
| `/v1/models` | `GET` | OpenAI-style model list with `ready` flags. |
| `/v1/audio/transcriptions` | `POST` | Multipart or JSON Base64 audio transcription. Use `response_format=verbose_json` for segments. |

## Multipart transcription fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `file` | binary | yes | Audio file such as wav, mp3, flac, m4a, ogg, or webm. |
| `model` | string | no | Defaults to `sensevoice`; `/v1/models` also lists `moss-transcribe-diarize` for offline long-form transcription with native anonymous speaker labels. |
| `language` | string | no | Optional language hint. |
| `response_format` | string | no | Use `json` or `verbose_json`. |

## JSON Base64 transcription fields

Send `Content-Type: application/json`. Put the Base64 audio in `audio_base64`, `file`, or `audio`; a `data:audio/...;base64,...` URI is also accepted.

| Field | Type | Required | Notes |
|---|---|---|---|
| `audio_base64` | string | yes* | Base64 audio; `file` or `audio` may be used instead. |
| `filename` | string | no | Original filename used to select the audio extension; defaults to `audio.wav`. |
| `model` | string | no | Defaults to `sensevoice`. |
| `language` | string | no | Optional language hint. |
| `response_format` | string | no | Use `json` or `verbose_json`. |

`*` Provide at least one of the three audio fields; JSON requests do not require a multipart upload object.

## Validate against a running server

```bash
cd examples/openai_api
python server.py --model sensevoice --device cuda --port 8000
curl -fsS http://localhost:8000/openapi.json > /tmp/funasr-openapi-live.json
```

The live FastAPI schema may include framework-specific validation details; this checked-in spec keeps the public integration surface small and stable.

MOSS requires its isolated Transformers environment. Start it with
`funasr-server --model moss-transcribe-diarize --device cuda:0` and request
`response_format=verbose_json`; see the [deployment guide](../../docs/moss_transcribe_diarize.md).
