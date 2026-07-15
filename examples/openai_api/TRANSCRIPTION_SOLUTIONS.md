# 实时转写与离线说话人分离方案

结论：实时当前最佳方案是 `2pass + 热词 + 只看 final`；离线最佳方案是 `Paraformer + VAD + 标点 + CAM++ 说话人分离`，离线再按 speaker/时间戳输出。

## 实时方案

适合低延迟字幕、会议/系统音频实时查看。当前 `live_caption_client.py` 没有稳定的说话人切换/说话人分离能力，建议实时阶段先追求识别质量和稳定分段。

```bash
cd /media/code/tools/FunASR/examples/openai_api

docker compose --profile websocket up -d funasr-ws

cat >/tmp/funasr-hotwords.txt <<'EOF'
Moinemap 40
franakency 40
gradient 30
director 30
EOF

rm -f /tmp/live-caption-events.jsonl

python live_caption_client.py \
  --source 'alsa_output.usb-HECATE_G2_GAMING_HEADSET_HECATE_G2_GAMING_HEADSET_20190403-00.analog-stereo.monitor' \
  --host game \
  --port 10096 \
  --mode 2pass \
  --partials never \
  --hotword /tmp/funasr-hotwords.txt \
  --jsonl /tmp/live-caption-events.jsonl
```

查看实时最终段结果：

```bash
jq -r 'select(.caption_type=="final" and .segment_text!="") | "[\(.start_sec) → \(.end_sec)] \(.segment_text)"' \
  /tmp/live-caption-events.jsonl
```

### 同步录音

建议实时转写时同步保存一份 WAV，后续用离线说话人分离重新整理文本。

```bash
ffmpeg -hide_banner -loglevel warning \
  -f pulse \
  -i 'alsa_output.usb-HECATE_G2_GAMING_HEADSET_HECATE_G2_GAMING_HEADSET_20190403-00.analog-stereo.monitor' \
  -vn -ac 1 -ar 16000 -c:a pcm_s16le \
  -y /tmp/live-caption.wav
```

## 离线说话人方案

适合会后整理、按说话人切段、生成更稳定的会议记录。这个方案使用 FunASR 离线模型，并启用 `spk_model="cam++"` 做 speaker diarization。

```bash
cd /media/code/tools/FunASR/examples/openai_api

docker compose build funasr-api

cat >/tmp/offline_spk.py <<'PY'
import json, os, sys
from pathlib import Path
from funasr import AutoModel

audio = sys.argv[1]
out_json = sys.argv[2]
out_txt = sys.argv[3]
hotword_path = sys.argv[4] if len(sys.argv) > 4 else ""

model = AutoModel(
    model="paraformer-zh",
    vad_model="fsmn-vad",
    punc_model="ct-punc",
    spk_model="cam++",
    vad_kwargs={"max_single_segment_time": 15000},
    device=os.getenv("FUNASR_DEVICE", "cpu"),
    disable_update=True,
)

kwargs = {"input": audio, "batch_size_s": 300}
if hotword_path and Path(hotword_path).exists():
    kwargs["hotword"] = Path(hotword_path).read_text(encoding="utf-8")

result = model.generate(**kwargs)[0]
Path(out_json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

lines = []
for seg in result.get("sentence_info", []):
    start = seg.get("start", 0) / 1000
    end = seg.get("end", 0) / 1000
    speaker = seg.get("spk", "?")
    text = seg.get("text", "").strip()
    if text:
        lines.append(f"[{start:8.2f} -> {end:8.2f}] S{speaker}: {text}")

Path(out_txt).write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))
PY

docker compose run --rm \
  -v /tmp:/data \
  -e FUNASR_DEVICE=cpu \
  funasr-api \

python /data/offline_spk.py \
    /data/live-caption.wav \
    /data/offline-spk.json \
    /data/offline-spk.txt \
    /data/funasr-hotwords.txt
```

输出文件：

- `/tmp/offline-spk.txt`：按时间 + 说话人分段的文本。
- `/tmp/offline-spk.json`：完整 FunASR 原始结果，含 `sentence_info`、`spk`、时间戳。

## 建议工作流

1. 实时字幕使用 WebSocket `2pass`，追求低延迟和可读性。
2. 同步保存 `/tmp/live-caption.wav`。
3. 会后跑离线 `cam++` 说话人分离。
4. 最终记录以 `/tmp/offline-spk.txt` 为准。

如果目标是“说话人切换后切成一段”，以离线结果为准；实时 WebSocket 当前更适合低延迟字幕，不适合稳定 speaker diarization。
