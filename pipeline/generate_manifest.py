"""
manifest.json を生成するスクリプト
audio/ にある WAV ファイルと scripts/ の JSON から episode リストを作る
"""

import re
import json
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
SOURCE_DIR = BASE_DIR / "source"
SCRIPTS_DIR = BASE_DIR / "scripts"
AUDIO_DIR = BASE_DIR / "audio"
PLAYER_DIR = BASE_DIR / "player"

load_dotenv(Path(__file__).resolve().parent / ".env")

SKIP_CHAPTERS = {1, 2, 3, 4, 5, 6, 7, 11, 32, 33, 44, 53, 58, 62, 66, 67}


def load_chapters(text):
    pattern = r'(\d+[-－]\d+[\.\．]\s*.+?)(?=\n\d+[-－]\d+[\.\．]|\Z)'
    matches = re.findall(pattern, text, re.DOTALL)
    chapters = {}
    for match in matches:
        first_line = match.strip().split('\n')[0]
        m = re.match(r'(\d+)[-－](\d+)', first_line)
        if not m:
            continue
        chap = m.group(1)
        if chap not in chapters:
            chapters[chap] = {"sections": []}
        chapters[chap]["sections"].append(match.strip())
    for chap, data in chapters.items():
        first_title = data["sections"][0].split('\n')[0].strip()
        title_m = re.match(r'\d+[-－]\d+[\.\．]\s*(.+)', first_title)
        data["title"] = title_m.group(1) if title_m else first_title
    return dict(sorted(chapters.items(), key=lambda x: int(x[0])))


def main():
    text = list(SOURCE_DIR.glob("*.txt"))[0].read_text(encoding="utf-8", errors="ignore")
    chapters = load_chapters(text)

    episodes = []
    for chap, data in chapters.items():
        if int(chap) in SKIP_CHAPTERS:
            continue
        wav_path = AUDIO_DIR / f"ep{int(chap):03d}.wav"
        episodes.append({
            "id": int(chap),
            "file": f"ep{int(chap):03d}.wav",
            "title": f"第{chap}章：{data['title']}",
            "available": wav_path.exists()
        })

    PLAYER_DIR.mkdir(exist_ok=True)
    manifest_path = PLAYER_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(episodes, ensure_ascii=False, indent=2))
    available = sum(1 for e in episodes if e["available"])
    print(f"manifest.json 生成完了: {len(episodes)}話中{available}話が利用可能")


if __name__ == "__main__":
    main()
