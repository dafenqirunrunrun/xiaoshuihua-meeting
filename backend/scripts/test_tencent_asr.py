"""用本地 PCM 文件测试腾讯云实时语音识别。"""

from __future__ import annotations

import pathlib
import sys

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.speech.tencent_client import (
    SLICE_INTERVAL,
    SLICE_SIZE,
    TencentAsrConfigError,
    TencentAsrServiceError,
    TencentRealtimeAsrClient,
    load_env_file,
)

PCM_PATH = BACKEND_ROOT / "test_data" / "test.pcm"


def main() -> int:
    load_env_file(BACKEND_ROOT / ".env")

    if not PCM_PATH.exists():
        print("[ASR ERROR]")
        print(f"message=测试音频不存在：{PCM_PATH}")
        print("请放置 16kHz / 16bit / mono / signed PCM little-endian 的 test.pcm。")
        return 1

    try:
        client = TencentRealtimeAsrClient()
        print(f"[ASR CONFIG] pcm={PCM_PATH}")
        print(f"[ASR CONFIG] engine={client.config.engine}")
        print(f"[ASR CONFIG] request_params={', '.join(client.request_parameter_names())}")
        print(f"[ASR CONFIG] slice_size={SLICE_SIZE} bytes")
        print(f"[ASR CONFIG] slice_interval={SLICE_INTERVAL} seconds")
        client.recognize_file(PCM_PATH)
    except TencentAsrConfigError as exc:
        print("[ASR ERROR]")
        print(f"message={exc}")
        return 2
    except TencentAsrServiceError as exc:
        print("[ASR ERROR]")
        print(f"voice_id={exc.voice_id}")
        print(f"code={exc.code}")
        print(f"message={exc.message}")
        return 3
    except Exception as exc:
        print("[ASR ERROR]")
        print("code=-1")
        print(f"message={exc}")
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
