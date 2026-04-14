"""
明鏡ポッドキャスト自動生成パイプライン

構成: 53章 → 53エピソード（各約10分）
     章内の全単元をまとめて1本の対談に変換

使い方:
  python pipeline.py --list                   # エピソード一覧
  python pipeline.py --episode 8              # 第8章をテスト生成
  python pipeline.py --tts voicevox           # 全章をVOICEVOXで生成
  python pipeline.py --episode 8 --tts elevenlabs  # ElevenLabsでテスト
  python pipeline.py --script-only            # 台本のみ（音声なし）
"""

import re
import os
import sys
import json
import argparse
import subprocess
import requests
from pathlib import Path
from dotenv import load_dotenv
import anthropic

# パス設定
BASE_DIR = Path(__file__).parent.parent
SOURCE_DIR = BASE_DIR / "source"
SCRIPTS_DIR = BASE_DIR / "scripts"
AUDIO_DIR = BASE_DIR / "audio"
ENV_FILE = Path(__file__).resolve().parent / ".env"

load_dotenv(ENV_FILE, override=True)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

# ポッドキャスト対象外の章（目次・特典・章見出しのみ・エンディング）
SKIP_CHAPTERS = {1, 2, 3, 4, 5, 6, 7, 11, 32, 33, 44, 53, 58, 62, 66, 67}

# 第8章専用の冒頭注記（第1回エピソード）
CHAPTER_8_INTRO = """【制作注記】この第8章は本ポッドキャストの第1回です。
第1〜7章は目次・購入特典・案内などの導入コンテンツのため、ポッドキャストでは省略しています。
台本の冒頭で「第1〜7章は案内や特典情報なので、実質的なマーケティングの学びはこの第8章からスタートします」という旨を自然に一言添えてください。
"""

# VOICEVOX スピーカーID
SPEAKER_HOST = 2   # 四国めたん（ノーマル）- 司会・質問役
SPEAKER_GUEST = 3  # ずんだもん（ノーマル）- 解説役

# ElevenLabs 声設定
EL_VOICE_HOST = "Xb7hH8MSUJpSbSDYk0k2"
EL_VOICE_GUEST = "pqHfZKP75CvOlQylNhV4"


# ── 1. テキスト解析 ───────────────────────────────────────────

def load_chapters(text: str) -> dict:
    """章ごとに全単元をまとめたdictを返す {章番号(str): {"title": str, "sections": [str]}}"""
    # XX-X. 形式のサブセクションを章ごとにグループ化
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

    # 章タイトルは最初の単元のタイトルから取得
    for chap, data in chapters.items():
        first_title = data["sections"][0].split('\n')[0].strip()
        title_m = re.match(r'\d+[-－]\d+[\.\．]\s*(.+)', first_title)
        data["title"] = title_m.group(1) if title_m else first_title

    # サブセクションがない章で本文があるものを追加（例: 67章）
    subsection_chap_nums = set(chapters.keys())
    chapter_headers = list(re.finditer(r'^(\d+)[\.\．]\s*(.+)', text, re.MULTILINE))
    for i, h in enumerate(chapter_headers):
        chap = h.group(1)
        title = h.group(2).strip()
        if chap in subsection_chap_nums:
            continue  # サブセクションありの章はスキップ
        # 次の章ヘッダーまでの本文を取得
        end = chapter_headers[i + 1].start() if i + 1 < len(chapter_headers) else len(text)
        body = text[h.start():end].strip()
        # 本文が100文字以上、かつタイトルが数字・記号始まりでないものだけ追加
        if len(body) >= 100 and not re.match(r'^[\d\.\%]', title) and int(chap) <= 67:
            chapters[chap] = {"title": title, "sections": [body]}

    return dict(sorted(chapters.items(), key=lambda x: int(x[0])))


# ── 2. 対談台本生成（Claude API）─────────────────────────────

DIALOGUE_PROMPT = """あなたはポッドキャスト台本ライターです。
以下のマーケティング教材の内容を、対談ポッドキャスト台本に変換してください。

【登場人物】
- めたん（語り役）: 優等生タイプ。内容をしっかり解説しながら、ずんだもんの的外れな反応にツッコミを入れる。です・ます調で丁寧に話す
- ずんだもん（聞き役）: はっちゃけたキャラ。カジュアルで砕けた話し方（〜なのだ、え マジで!?、それ最高じゃん！など）。ときどき的外れなことを言ってめたんにツッコまれる。テンション高め

【ルール】
- 冒頭1行目は必ずめたんの「今日は明鏡の第{chapter_num}章『{chapter_title}』についてお話しします。」から始める
- 15〜20往復の会話
- 難しい概念は具体例・たとえ話で噛み砕く
- めたんは解説しながらも、ずんだもんの反応にツッコむ場面を作る
- ずんだもんは「〜なのだ！」「え、それってつまり〜ってこと!?」「めちゃくちゃ大事じゃん！」のような話し方
- 各セリフは1〜3文程度（長すぎない）
- 末尾はめたんが軽くまとめ、最後に「今日の問い：〜」という形でリスナーへの問いかけを1つ投げかけて締める
- 出力形式は以下のJSONのみ（前後の説明・コードブロック不要）:
[
  {{"speaker": "めたん", "text": "..."}},
  {{"speaker": "ずんだもん", "text": "..."}},
  ...
]

【元テキスト】
{section_texts}
"""

def generate_dialogue(chapter_num: str, chapter_title: str, sections: list) -> list:
    combined = "\n\n".join(sections)
    extra = CHAPTER_8_INTRO if chapter_num == "8" else ""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=3000,
        messages=[{"role": "user", "content": DIALOGUE_PROMPT.format(
            chapter_num=chapter_num,
            chapter_title=chapter_title,
            section_texts=extra + combined
        )}]
    )
    raw = message.content[0].text.strip()
    raw = re.sub(r'^```(?:json)?\n?', '', raw)
    raw = re.sub(r'\n?```$', '', raw)
    return json.loads(raw)


# ── 3. 音声生成 ───────────────────────────────────────────────

def tts_voicevox(text: str, speaker_id: int, out_path: Path):
    base = "http://localhost:50021"
    query = requests.post(f"{base}/audio_query", params={"text": text, "speaker": speaker_id}).json()
    wav = requests.post(f"{base}/synthesis", params={"speaker": speaker_id}, json=query).content
    out_path.write_bytes(wav)


def tts_elevenlabs(text: str, voice_id: str, out_path: Path):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    body = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
    }
    resp = requests.post(url, headers=headers, json=body)
    out_path.write_bytes(resp.content)


def generate_audio(dialogue: list, episode_key: str, tts: str) -> Path:
    tmp_dir = AUDIO_DIR / f"tmp_{episode_key}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    file_list = tmp_dir / "files.txt"
    parts = []

    speaker_map = {
        "めたん": (SPEAKER_HOST, EL_VOICE_HOST),
        "ずんだもん": (SPEAKER_GUEST, EL_VOICE_GUEST),
    }

    for i, line in enumerate(dialogue):
        speaker = line["speaker"]
        text = line["text"]
        vv_id, el_id = speaker_map.get(speaker, (SPEAKER_GUEST, EL_VOICE_GUEST))

        if tts == "voicevox":
            part_path = tmp_dir / f"{i:03d}.wav"
            tts_voicevox(text, vv_id, part_path)
        else:
            part_path = tmp_dir / f"{i:03d}.mp3"
            tts_elevenlabs(text, el_id, part_path)
        parts.append(part_path)
        print(f"  [{i+1}/{len(dialogue)}] {speaker}: {text[:25]}...")

    with open(file_list, "w") as f:
        for p in parts:
            f.write(f"file '{p.resolve()}'\n")

    ext = "wav" if tts == "voicevox" else "mp3"
    out_path = AUDIO_DIR / f"ep{int(episode_key):03d}.{ext}"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(file_list), str(out_path)],
        check=True, capture_output=True
    )
    return out_path


# ── 4. メイン処理 ─────────────────────────────────────────────

def process_episode(chap: str, data: dict, tts: str, script_only: bool):
    key = f"ep{int(chap):03d}"
    print(f"\n=== 第{chap}章: {data['title']} ({len(data['sections'])}単元) ===")

    script_path = SCRIPTS_DIR / f"{key}.json"
    if script_path.exists():
        print("  台本キャッシュあり")
        dialogue = json.loads(script_path.read_text())
    else:
        print("  台本生成中...")
        dialogue = generate_dialogue(chap, data["title"], data["sections"])
        script_path.write_text(json.dumps(dialogue, ensure_ascii=False, indent=2))
        print(f"  台本保存: {script_path.name} ({len(dialogue)}行)")

    if script_only:
        return

    ext = "wav" if tts == "voicevox" else "mp3"
    audio_path = AUDIO_DIR / f"ep{int(chap):03d}.{ext}"
    if audio_path.exists():
        print(f"  音声キャッシュあり → スキップ")
        return

    print("  音声生成中...")
    out = generate_audio(dialogue, chap, tts)
    print(f"  完成: {out.name}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tts", choices=["voicevox", "elevenlabs"], default="voicevox")
    parser.add_argument("--episode", help="特定の章番号（例: 8）")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--script-only", action="store_true", help="台本生成のみ")
    args = parser.parse_args()

    source_files = list(SOURCE_DIR.glob("*.txt"))
    if not source_files:
        print("ERROR: source/ に .txt ファイルがありません")
        sys.exit(1)
    text = source_files[0].read_text(encoding="utf-8", errors="ignore")
    chapters = load_chapters(text)

    if args.list:
        print(f"エピソード数: {len(chapters)}")
        for chap, data in chapters.items():
            sec_count = len(data["sections"])
            total_chars = sum(len(s) for s in data["sections"])
            print(f"  第{chap:>2}章 ({sec_count}単元 / {total_chars}文字): {data['title']}")
        return

    SCRIPTS_DIR.mkdir(exist_ok=True)
    AUDIO_DIR.mkdir(exist_ok=True)

    if args.episode:
        if args.episode not in chapters:
            print(f"ERROR: 第{args.episode}章が見つかりません")
            sys.exit(1)
        targets = {args.episode: chapters[args.episode]}
    else:
        targets = {k: v for k, v in chapters.items() if int(k) not in SKIP_CHAPTERS}

    for chap, data in targets.items():
        process_episode(chap, data, args.tts, args.script_only)

    print("\n完了！")


if __name__ == "__main__":
    main()
