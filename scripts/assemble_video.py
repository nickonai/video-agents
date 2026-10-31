#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сборка ролика из снятых планов.

Инструмент агента «Монтажёр»: берёт готовые mp4 из output/, склеивает их в
нужном порядке и кладёт результат рядом. Печатает каждый шаг — на эфире
зритель должен видеть, что происходит.

    python3 scripts/assemble_video.py --clips avoska-light avoska-capacity \\
        --name avoska-reel --transition 0.4

Склейка идёт через xfade: планы перетекают друг в друга, звук —
через acrossfade. Жёсткий стык — `--transition 0`.

⚠️ Планы должны быть одного формата: смешивать 9:16 и 16:9 в одной сборке
   нельзя, монтажёр остановится и скажет об этом.

Сборка локальная, платных вызовов нет. `--dry-run` читает планы, проверяет
формат и переход и печатает итог, не запуская ffmpeg.

Код выхода: 0 — ролик собран; иной — сборка не состоялась. При ошибке ffmpeg в
консоль идут последние строки, полный журнал лежит в output/<name>.ffmpeg.log.
Ролик пишется во временный файл и появляется под своим именем только целым.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"
FPS = 24                      # Seedance отдаёт 24 кадра, держим то же
AUDIO_RATE = 48000            # приводим дорожки к одному, иначе acrossfade врёт
PROBE_TIMEOUT = 60            # секунды на чтение одного плана
ENCODE_TIMEOUT = 1800         # секунды на склейку; зависший ffmpeg не держит сессию
ERROR_LINES = 12              # строк stderr в консоль; остальное — в журнал


def run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Запуск без stdin (ffmpeg иначе ждёт клавиш) и с потолком по времени."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False,
                              stdin=subprocess.DEVNULL, timeout=timeout)
    except FileNotFoundError:
        sys.exit(f"❌ Не найден {cmd[0]}: установите ffmpeg (macOS: brew install ffmpeg)")
    except subprocess.TimeoutExpired:
        sys.exit(f"❌ {cmd[0]} не уложился в {timeout} с и остановлен")


def probe(path: Path) -> dict:
    """Длительность, размер кадра и наличие звука — по ним собираем фильтр."""
    res = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(path)], PROBE_TIMEOUT)
    if res.returncode != 0:
        tail = "\n".join(res.stderr.strip().splitlines()[-ERROR_LINES:])
        sys.exit(f"❌ Не читается {path.name}: {tail}")
    data = json.loads(res.stdout)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        sys.exit(f"❌ В {path.name} нет видеодорожки")
    audio = any(s["codec_type"] == "audio" for s in data["streams"])
    return {"duration": float(data["format"]["duration"]),
            "width": int(video["width"]), "height": int(video["height"]),
            "audio": audio}


def resolve(name: str) -> Path:
    """Имя плана → файл в output/. Принимаем и slug, и путь."""
    for candidate in (Path(name), OUTPUT / name, OUTPUT / f"{name}.mp4"):
        if candidate.is_file():
            return candidate
    sys.exit(f"❌ Нет такого плана: {name}. Проверьте output/")


def build_filter(clips: list[dict], fade: float, audio: bool) -> tuple[str, str, str]:
    """Цепочка xfade: каждый следующий план наезжает на хвост предыдущего."""
    parts = []
    for i, clip in enumerate(clips):
        # Общий знаменатель: fps, пиксели, таймбаза. Без этого xfade рассыпается.
        parts.append(f"[{i}:v]fps={FPS},format=yuv420p,settb=AVTB[v{i}]")

    current, offset = "v0", clips[0]["duration"] - fade
    for i in range(1, len(clips)):
        label = f"vx{i}"
        parts.append(f"[{current}][v{i}]xfade=transition=fade:"
                     f"duration={fade}:offset={offset:.3f}[{label}]")
        current = label
        # Каждая склейка съедает fade секунд: следующий стык считаем от неё
        offset += clips[i]["duration"] - fade

    audio_out = ""
    if audio:
        for i in range(len(clips)):
            parts.append(f"[{i}:a]aformat=sample_rates={AUDIO_RATE}:"
                         f"channel_layouts=stereo[a{i}]")
        current_a = "a0"
        for i in range(1, len(clips)):
            label = f"ax{i}"
            parts.append(f"[{current_a}][a{i}]acrossfade=d={fade}:c1=tri:c2=tri[{label}]")
            current_a = label
        audio_out = current_a

    return ";".join(parts), current, audio_out


def build_concat(clips: list[dict], audio: bool) -> tuple[str, str, str]:
    """Жёсткий стык: без перехода, встык."""
    parts = []
    for i in range(len(clips)):
        parts.append(f"[{i}:v]fps={FPS},format=yuv420p,settb=AVTB[v{i}]")
        if audio:
            parts.append(f"[{i}:a]aformat=sample_rates={AUDIO_RATE}:"
                         f"channel_layouts=stereo[a{i}]")
    streams = "".join(f"[v{i}][a{i}]" if audio else f"[v{i}]"
                      for i in range(len(clips)))
    parts.append(f"{streams}concat=n={len(clips)}:v=1:a={1 if audio else 0}"
                 f"[vout]{'[aout]' if audio else ''}")
    return ";".join(parts), "vout", "aout" if audio else ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Сборка рекламного ролика из снятых планов")
    ap.add_argument("--clips", nargs="+", required=True,
                    help="планы по порядку: slug или путь к mp4")
    ap.add_argument("--name", default="montage", help="имя итогового файла без расширения")
    ap.add_argument("--transition", type=float, default=0.4,
                    help="секунды перетекания между планами; 0 — жёсткий стык")
    ap.add_argument("--audio", default="keep", choices=["keep", "mute"],
                    help="keep — родной звук планов, mute — тишина под озвучку")
    ap.add_argument("--dry-run", action="store_true",
                    help="проверить планы и параметры, не запуская ffmpeg")
    args = ap.parse_args()

    paths = [resolve(name) for name in args.clips]
    if len(paths) < 2:
        sys.exit("❌ Для сборки нужно хотя бы два плана")

    print(f"🎞  Монтаж: {len(paths)} плана")
    clips = []
    for path in paths:
        info = probe(path)
        info["path"] = path
        clips.append(info)
        print(f"   {path.name}: {info['duration']:.2f} с · "
              f"{info['width']}×{info['height']} · "
              f"звук: {'есть' if info['audio'] else 'нет'}")

    # Разный формат кадра — это не монтаж, а брак. Останавливаемся здесь.
    sizes = {(c["width"], c["height"]) for c in clips}
    if len(sizes) > 1:
        sys.exit(f"❌ Планы разного формата: {sizes}. "
                 "Собирать вместе можно только кадры одного размера")

    audio = args.audio == "keep" and all(c["audio"] for c in clips)
    if args.audio == "keep" and not audio:
        print("   ⚠️  Не у всех планов есть звук — собираем без звука")

    fade = args.transition
    if fade > 0:
        # Переход длиннее самого короткого плана съест его целиком
        shortest = min(c["duration"] for c in clips)
        if fade >= shortest:
            sys.exit(f"❌ Переход {fade} с длиннее самого короткого плана "
                     f"({shortest:.2f} с). Уменьшите --transition")
        graph, vout, aout = build_filter(clips, fade, audio)
        total = sum(c["duration"] for c in clips) - fade * (len(clips) - 1)
        print(f"   Склейка: перетекание {fade} с, итог ≈ {total:.2f} с")
    else:
        graph, vout, aout = build_concat(clips, audio)
        total = sum(c["duration"] for c in clips)
        print(f"   Склейка: жёсткий стык, итог ≈ {total:.2f} с")

    if args.dry_run:
        print("🧪 Пробный прогон: ffmpeg не запускался, файл не создан")
        return

    target = OUTPUT / f"{args.name}.mp4"
    partial = OUTPUT / f"{args.name}.partial.mp4"
    cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error"]
    for clip in clips:
        cmd += ["-i", str(clip["path"])]
    cmd += ["-filter_complex", graph, "-map", f"[{vout}]"]
    if audio:
        cmd += ["-map", f"[{aout}]", "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(partial)]

    print("🎬 Собираю…", flush=True)
    started = time.time()
    res = run(cmd, ENCODE_TIMEOUT)
    if res.returncode != 0:
        partial.unlink(missing_ok=True)
        full = OUTPUT / f"{args.name}.ffmpeg.log"
        full.write_text(res.stderr, encoding="utf-8")
        tail = "\n".join(res.stderr.strip().splitlines()[-ERROR_LINES:])
        sys.exit(f"❌ ffmpeg не справился (код {res.returncode}):\n{tail}\n"
                 f"   Полный журнал: {full}")
    partial.replace(target)

    seconds = time.time() - started
    final = probe(target)
    log = {"clips": [c["path"].name for c in clips],
           "transition": fade, "audio": audio,
           "duration": final["duration"],
           "size": f"{final['width']}×{final['height']}",
           "seconds": round(seconds, 1)}
    target.with_suffix(".mp4.cut-log.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")

    size_mb = target.stat().st_size / 1024 / 1024
    print(f"✅ Готово за {seconds:.1f} с: {target}")
    print(f"   {final['duration']:.2f} с · {final['width']}×{final['height']} · "
          f"{size_mb:.1f} МБ · звук: {'есть' if audio else 'нет'}")


if __name__ == "__main__":
    main()
