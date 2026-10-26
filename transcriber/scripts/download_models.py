"""Заранее скачать веса моделей распознавания в MODEL_DIR (по умолчанию ~/.cache/gigaam).

Нужно для установки на сервер без интернета: образ собирается с PRELOAD_MODELS=1 там, где интернет есть,
и веса едут внутри образа. Без этого сервис скачает их сам при первом запуске.
LLM (GigaChat) сюда не входит — её хранит и скачивает Ollama.
Запуск: python transcriber/scripts/download_models.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings  # noqa: E402


def main() -> None:
    import gigaam
    from silero_vad import load_silero_vad

    s = Settings()
    root = s.model_dir or None
    names = [s.asr_model] + (["emo"] if s.emotion_enabled else [])
    for name in names:
        gigaam.load_model(name, device="cpu", download_root=root)
        print(f"{name}: готово ({root or '~/.cache/gigaam'})", flush=True)
    load_silero_vad()  # веса VAD лежат внутри pip-пакета, проверяем, что грузится
    print("silero-vad: готово", flush=True)


if __name__ == "__main__":
    main()
