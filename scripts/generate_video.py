#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Съёмка ролика на Seedance 2.5 через Replicate.

Инструмент агента «Оператор»: берёт готовый промпт, запускает генерацию, ждёт
результат, кладёт mp4 в output/ и пишет рядом run-log.json. Печатает каждый
шаг — на эфире зритель должен видеть, что происходит.

    python3 scripts/generate_video.py --prompt-file briefs/kofeynya.txt \\
        --name kofeynya --duration 5 --aspect 9:16 --audio true

⛔ Фотореалистичное лицо во входной картинке Replicate режет (E005).
   Нарисованное лицо проходит, человека можно задать текстом.
   Подробности — references/seedance-2.5.md.

Токен: REPLICATE_API_TOKEN или AI_STUDIO__REPLICATE_API_TOKEN в окружении,
иначе .env рядом или ~/unika/backend/.env.univerus.local.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

API = "https://api.replicate.com/v1"
MODEL = "bytedance/seedance-2.5"
PROMPT_LIMIT = 2000          # у 2.5 жёсткий предел; 2.0 держал 4000
POLL_SEC = 8
ROOT = Path(__file__).resolve().parent.parent
ENV_FILES = [ROOT / ".env", Path.home() / "unika/backend/.env.univerus.local"]
ENV_KEYS = ("REPLICATE_API_TOKEN", "AI_STUDIO__REPLICATE_API_TOKEN")


def token() -> str:
    for key in ENV_KEYS:
        if os.environ.get(key):
            return os.environ[key].strip()
    for path in ENV_FILES:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            for key in ENV_KEYS:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("❌ Нет токена Replicate: задайте REPLICATE_API_TOKEN или проверьте .env")


TOKEN = token()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def api(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    last: Exception | None = None
    for attempt in range(4):                       # сеть иногда рвётся, повторяем
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except Exception as exc:                   # noqa: BLE001 — печатаем и пробуем снова
            last = exc
            print(f"  … повтор {attempt + 1}: {exc}", flush=True)
            time.sleep(4)
    raise last                                     # type: ignore[misc]


def upload(path: str) -> str:
    """Локальный файл → ссылка Replicate Files. Нужен для опорных кадров."""
    for attempt in range(4):
        run = subprocess.run(
            ["curl", "-s", "-X", "POST", f"{API}/files",
             "-H", f"Authorization: Bearer {TOKEN}", "-F", f"content=@{path}"],
            capture_output=True, text=True, check=False)
        try:
            return json.loads(run.stdout)["urls"]["get"]
        except (json.JSONDecodeError, KeyError):
            print(f"  … повтор загрузки {attempt + 1}: {Path(path).name}", flush=True)
            time.sleep(3)
    sys.exit(f"❌ Не удалось загрузить {path}")


def resolve(value: str) -> str:
    return upload(value) if value and Path(value).exists() else value


def main() -> None:
    ap = argparse.ArgumentParser(description="Seedance 2.5 · съёмка рекламного ролика")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--prompt", help="текст сцены")
    src.add_argument("--prompt-file", help="файл с текстом сцены")
    ap.add_argument("--name", default="video", help="имя файла без расширения")
    ap.add_argument("--duration", type=int, default=5, help="секунды, 4–30")
    ap.add_argument("--resolution", default="720p", choices=["480p", "720p"])
    ap.add_argument("--aspect", default="9:16",
                    choices=["9:16", "16:9", "1:1", "4:3", "3:4", "21:9", "adaptive"])
    ap.add_argument("--audio", default="true", choices=["true", "false"],
                    help="true — звук сцены, false — если поверх ляжет озвучка")
    ap.add_argument("--image", default="", help="опорный первый кадр (без фото лиц)")
    ap.add_argument("--last-frame", default="", help="опорный последний кадр")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    prompt = (Path(args.prompt_file).read_text(encoding="utf-8").strip()
              if args.prompt_file else args.prompt.strip())
    if len(prompt) > PROMPT_LIMIT:
        sys.exit(f"❌ Промпт {len(prompt)} символов, предел Seedance 2.5 — {PROMPT_LIMIT}. "
                 "Сократить, а не дописывать в конец")
    if not 4 <= args.duration <= 30:
        sys.exit("❌ Длительность вне диапазона 4–30 секунд")
    if args.last_frame and not args.image:
        sys.exit("❌ Последний кадр работает только вместе с первым (--image)")

    print(f"🎬 Модель: {MODEL}")
    print(f"   Формат {args.aspect} · {args.resolution} · {args.duration} с · "
          f"звук: {'да' if args.audio == 'true' else 'нет'}")
    print(f"   Сцена: {prompt[:110]}{'…' if len(prompt) > 110 else ''}")

    payload = {"prompt": prompt, "duration": args.duration, "resolution": args.resolution,
               "aspect_ratio": args.aspect, "generate_audio": args.audio == "true",
               "watermark": False}
    if args.seed is not None:
        payload["seed"] = args.seed
    if args.image:
        payload["image"] = resolve(args.image)
    if args.last_frame:
        payload["last_frame_image"] = resolve(args.last_frame)

    pred = api("POST", f"{API}/models/{MODEL}/predictions", {"input": payload})
    pid, status = pred["id"], pred["status"]
    print(f"🚀 Запущено: {pid}")

    started = time.time()
    while status not in ("succeeded", "failed", "canceled"):
        time.sleep(POLL_SEC)
        try:
            pred = api("GET", f"{API}/predictions/{pid}")
            status = pred["status"]
        except Exception as exc:                   # noqa: BLE001
            print(f"  … опрос не прошёл: {exc}", flush=True)
            continue
        print(f"   [{int(time.time() - started)} с] {status}", flush=True)

    if status != "succeeded":
        err = str(pred.get("error"))
        print(f"❌ Не получилось: {err}")
        if "E005" in err or "sensitive" in err:
            print("   E005 — на входе фотореалистичное лицо. Нарисованное лицо проходит; "
                  "человека можно задать текстом. См. references/seedance-2.5.md")
        sys.exit(1)

    out = pred["output"]
    url = out if isinstance(out, str) else out[0]
    target = ROOT / "output" / f"{args.name}.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, target)
    log = {"id": pid, "model": MODEL, "prompt": prompt,
           "params": {k: v for k, v in payload.items() if k != "prompt"},
           "seconds": int(time.time() - started), "output_url": url}
    target.with_suffix(".mp4.run-log.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ Готово за {log['seconds']} с: {target}")


if __name__ == "__main__":
    main()
