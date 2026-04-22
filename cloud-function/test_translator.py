"""Local test for translator. Reads GEMINI_API_KEY from .env file."""

import os
import sys
from pathlib import Path
from translator import translate_summaries

sys.stdout.reconfigure(encoding="utf-8")


def load_env(path=".env"):
    p = Path(__file__).parent / path
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


load_env()

if not os.environ.get("GEMINI_API_KEY"):
    print("ERROR: GEMINI_API_KEY missing in .env")
    raise SystemExit(1)

samples = [
    "Bị phạt 500 triệu do xả thải vượt chuẩn tại nhà máy Dung Quất",
    "Khởi tố vụ án thất thoát vốn tại Vinashin",
    "Tai nạn lao động làm 3 công nhân tử vong tại công trường Hòa Phát",
    "UBCKNN xử phạt VinaCapital 200 triệu vì vi phạm công bố thông tin",
    "Bộ TN&MT thanh tra hoạt động khai thác khoáng sản trái phép tại Lào Cai",
    "Cháy lớn tại nhà máy DGC Lào Cai, thiệt hại hàng chục tỷ đồng",
    "Vinamilk bị tố xả nước thải chưa qua xử lý ra sông Sài Gòn",
]

result = translate_summaries(samples)

print("\n=== TRANSLATION RESULTS ===\n")
for vi, en in zip(samples, result):
    print(f"VI: {vi}")
    print(f"EN: {en}\n")
