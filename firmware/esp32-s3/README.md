# ESP32-S3 固件

这里是水花会议助手的 ESP32-S3 固件工程，使用 ESP-IDF 开发。当前阶段保留官方 Hello World 最小程序作为硬件 bring-up 基线，用于验证编译、烧录、串口监视和芯片运行环境。

后续模块边界：

- `microphone`：IM73D122 收音和 PDM 到 PCM 采集链路。
- `wifi`：ESP32-S3 Wi-Fi 连接和网络状态。
- `audio_upload`：PCM 音频上传到 FastAPI。
- `device_control`：开始采集、停止采集、心跳和设备状态。

基本命令：

```bash
idf.py set-target esp32s3
idf.py build
idf.py -p COMx flash monitor
```
