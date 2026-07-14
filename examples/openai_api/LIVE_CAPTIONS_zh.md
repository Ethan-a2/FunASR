# FunASR Runtime 实时字幕客户端

`live_caption_client.py` 会把本机系统声音或音频文件转成 `s16le/16k/mono`，通过 WebSocket 推给 FunASR Runtime 2pass 服务，并在终端实时打印 `2pass-online` 与最终修正文本。

## 前置条件

```bash
docker compose --profile websocket up -d funasr-ws
docker compose --profile websocket ps funasr-ws
pip install websockets
```

Linux 桌面系统声音采集还需要 `ffmpeg` 和 `pactl`。脚本默认连接 `ws://127.0.0.1:10096`，对应 `.env` 中的 `FUNASR_WS_HOST_PORT`。

## 用音频文件验证

```bash
ffmpeg -y -i /home/bay/rec/edge.mp3 -ac 1 -ar 16000 /tmp/edge.wav
python live_caption_client.py \
  --audio-file /tmp/edge.wav \
  --host 127.0.0.1 \
  --port 10096 \
  --mode 2pass \
  --jsonl /tmp/live-caption-events.jsonl
```

正常输出会先出现在线增量字幕，例如 `online 你好我是...`，停顿或文件结束后出现 `final 你好，我是...`。`--jsonl` 会保存每条服务端消息，便于后续接网页或字幕渲染。

## 采集系统声音

先列出可用的 PulseAudio/PipeWire 声源：

```bash
python live_caption_client.py --list-sources
```

选择以 `.monitor` 结尾的系统输出监听源，例如：

```bash
python live_caption_client.py \
  --source 'alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__HDMI1__sink.monitor' \
  --duration 60 \
  --jsonl /tmp/live-caption-events.jsonl
```

如果不传 `--source`，脚本会优先使用默认输出设备的 `<default-sink>.monitor`。播放会议音频后，终端通常会在约 `0.6-2s` 内看到 `online` 字幕，语音停顿后看到 `final` 修正文本。

## 常用参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | WebSocket 服务地址 |
| `--port` | `10096` | 本机映射端口 |
| `--mode` | `2pass` | `online`、`offline` 或 `2pass` |
| `--chunk-size` | `5,10,5` | Runtime 流式分块配置 |
| `--duration` | 不限制 | 采集 N 秒后自动停止 |
| `--jsonl` | 不写文件 | 追加保存字幕事件 |
| `--hotword` | 空 | 热词字符串或热词文件路径 |

## 故障排查

| 现象 | 处理方式 |
|---|---|
| 无法连接 WebSocket | 确认 `docker compose --profile websocket ps funasr-ws` 显示 `Up`，且端口是 `10096->10095`。 |
| 没有系统声音字幕 | 先用 `python live_caption_client.py --audio-file /tmp/edge.wav` 验证服务，再确认播放器输出设备和 `.monitor` 源一致。 |
| `pactl` 连接失败 | 在桌面用户会话中运行脚本，或手动传入可访问的 PulseAudio/PipeWire source。 |
| 缺少 Python 依赖 | 运行 `pip install websockets`。 |
