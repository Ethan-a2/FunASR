#!/usr/bin/env bash
set -euo pipefail

cd /workspace/FunASR/runtime/websocket/build/bin

decoder_threads="${FUNASR_WS_DECODER_THREADS:-$(nproc)}"
io_threads="${FUNASR_WS_IO_THREADS:-$(( (decoder_threads + 15) / 16 ))}"
model_threads="${FUNASR_WS_MODEL_THREADS:-1}"

exec ./funasr-wss-server-2pass \
  --download-model-dir /workspace/models \
  --model-dir "${FUNASR_WS_MODEL_DIR}" \
  --online-model-dir "${FUNASR_WS_ONLINE_MODEL_DIR}" \
  --vad-dir "${FUNASR_WS_VAD_DIR}" \
  --punc-dir "${FUNASR_WS_PUNC_DIR}" \
  --lm-dir "${FUNASR_WS_LM_DIR}" \
  --itn-dir "${FUNASR_WS_ITN_DIR}" \
  --decoder-thread-num "${decoder_threads}" \
  --model-thread-num "${model_threads}" \
  --io-thread-num "${io_threads}" \
  --port 10095 \
  --certfile "" \
  --keyfile "" \
  --hotword /workspace/models/hotwords.txt
