"""
B 站封面图生成：调用 MiniMax image-01 接口生成横版 + 竖版封面。

使用：
  set MINIMAX_API_KEY=eyJhbGciOi...
  python generate_cover.py

输出：
  webide-platform/scripts/cover/output/cover_16x9.jpeg
  webide-platform/scripts/cover/output/cover_9x16.jpeg
"""
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

API_URL = "https://api.minimax.cn/v1/image_generation"
MODEL = "image-01"

# 方案 C「成果直给风」封面 prompt —— 纯视觉，无文字（文字后期 PS 加）
PROMPT_16X9 = (
    "A clean minimalist developer desktop scene, "
    "center a translucent holographic Kubernetes wheel glowing in soft cyan, "
    "in front of it a sleek dark browser window showing a VSCode-style editor "
    "with colorful syntax-highlighted code lines, "
    "left side a small real-looking mini PC tower (like Intel NUC) with subtle blue LED, "
    "dark navy gradient background with faint dot grid pattern, "
    "top-right area left empty as text safe zone, "
    "bottom a thin glowing line connecting mini PC to kubernetes wheel to browser, "
    "flat modern tech illustration style, soft neon accents, "
    "no text, no watermark, no people, 16:9 composition, ultra sharp"
)

PROMPT_9X16 = (
    "Same scene as horizontal but vertically composed, "
    "mini PC tower at bottom, kubernetes wheel rising in the middle as glowing ring, "
    "browser with VSCode editor floating at top, "
    "dark gradient background with ascending code particles, "
    "top area kept clean and dark for title overlay, "
    "minimalist flat illustration, soft cyan and purple neon, "
    "no text, no watermark, 9:16 vertical composition, ultra sharp"
)


def generate(prompt: str, aspect_ratio: str, out_path: Path) -> None:
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        print("[ERROR] MINIMAX_API_KEY env var is not set.", file=sys.stderr)
        print("        set MINIMAX_API_KEY=eyJhbGciOi... then re-run.", file=sys.stderr)
        sys.exit(1)

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "response_format": "base64",
    }

    print(f"[INFO] POST {API_URL}  aspect={aspect_ratio}  prompt_len={len(prompt)}")
    # 用 curl 走 HTTPS：避免本机 Python OpenSSL 证书库问题
    result = subprocess.run(
        [
            "curl",
            "-sS",
            "-X", "POST", API_URL,
            "-H", f"Authorization: Bearer {api_key}",
            "-H", "Content-Type: application/json",
            "--data", json.dumps(payload),
            "--max-time", "180",
        ],
        capture_output=True,
        text=True,
        timeout=200,
    )
    if result.returncode != 0:
        print(f"[ERROR] curl failed (code {result.returncode}): {result.stderr}", file=sys.stderr)
        sys.exit(2)
    try:
        body = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"[ERROR] non-JSON response: {result.stdout[:500]}", file=sys.stderr)
        sys.exit(2)

    if "data" not in body or "image_base64" not in body.get("data", {}):
        print(f"[ERROR] No image in response: {body}", file=sys.stderr)
        sys.exit(3)

    images = body["data"]["image_base64"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode(images[0]))
    print(f"[OK]    {out_path}  ({out_path.stat().st_size / 1024:.1f} KB)")


def main() -> None:
    here = Path(__file__).parent
    out_dir = here / "output"
    generate(PROMPT_16X9, "16:9", out_dir / "cover_16x9.jpeg")
    generate(PROMPT_9X16, "9:16", out_dir / "cover_9x16.jpeg")


if __name__ == "__main__":
    main()
