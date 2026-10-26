"""Генерирует тестовые записи в samples/ локальным Silero TTS (без облака).

  call_stereo.m4a  — стерео: L = клиент, R = оператор (собрано ffmpeg из двух моно-дорожек)
  call_mono.mp3    — тот же принцип, но обе дорожки смешаны в моно
  speech_real.ogg  — реальная (не синтезированная) речь: пример из репозитория GigaAM

Silero TTS — лицензия CC BY-NC-SA, используется только для тестовых данных.
Запуск: python transcriber/scripts/make_samples.py [папка_samples]
"""
import subprocess
import sys
import tempfile
import urllib.request
import wave
from pathlib import Path

import numpy as np
import torch

TTS_RATE = 24000
GAP_S = 0.7
LONG_EXAMPLE_URL = "https://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM/long_example.wav"

# (роль, текст). Числа прописью: TTS их не раскрывает.
DIALOG_STEREO = [
    ("operator", "Сервисный центр, меня зовут Ксения, слушаю вас."),
    ("client", "Здравствуйте. У нас в магазине на Садовой перестал работать кассовый аппарат, "
               "при включении пишет ошибку фискального накопителя."),
    ("operator", "Понимаю. Подскажите, пожалуйста, название организации и номер договора."),
    ("client", "Общество Ромашка, договор номер сто двадцать пять."),
    ("operator", "Спасибо, нашла. Касса модели Атол, верно? Когда появилась ошибка?"),
    ("client", "Да, Атол. Сегодня утром, вчера вечером всё работало нормально. "
               "Сейчас мы не можем пробивать чеки, покупатели уходят."),
    ("operator", "Попробуйте перезагрузить кассу и проверить, что кабель питания плотно подключён."),
    ("client", "Уже перезагружали два раза, не помогает."),
    ("operator", "Хорошо. Я оформляю заявку на выезд инженера, он приедет сегодня до шести вечера. "
                 "Номер заявки вам придёт в смс."),
    ("client", "Отлично, спасибо. А можно, чтобы инженер позвонил перед приездом?"),
    ("operator", "Да, я указала это в заявке. Инженер позвонит за полчаса. Что-нибудь ещё?"),
    ("client", "Нет, это всё. До свидания."),
    ("operator", "Всего доброго, хорошего дня."),
]

DIALOG_MONO = [
    ("operator", "Добрый день, служба доставки, оператор Евгений."),
    ("client", "Здравствуйте. Я заказывала стиральную машину, доставка была назначена на вчера, "
               "но никто так и не приехал."),
    ("operator", "Приношу извинения. Назовите, пожалуйста, номер заказа."),
    ("client", "Номер заказа четыре пять семь восемь."),
    ("operator", "Вижу ваш заказ. К сожалению, машина сломалась в пути, и доставку перенесли. "
                 "Вас должны были предупредить."),
    ("client", "Мне никто не звонил. Я весь день ждала дома и отпросилась с работы."),
    ("operator", "Ещё раз извините. Могу предложить доставку завтра с десяти до двенадцати "
                 "и бесплатный подъём на этаж в качестве компенсации."),
    ("client", "Хорошо, завтра утром меня устроит."),
    ("operator", "Записал. Завтра с десяти до двенадцати, подъём бесплатно. "
                 "Водитель позвонит за час до приезда."),
    ("client", "Спасибо, буду ждать."),
]

VOICES = {
    "stereo": {"client": "aidar", "operator": "xenia"},
    "mono": {"client": "baya", "operator": "eugene"},
}


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def build_tracks(model, dialog, voices) -> dict[str, np.ndarray]:
    """Две дорожки одинаковой длины: в каждой говорит только своя роль, на месте чужих реплик — тишина."""
    gap = np.zeros(int(GAP_S * TTS_RATE), dtype=np.float32)
    tracks = {"client": [gap], "operator": [gap]}
    for role, text in dialog:
        speech = model.apply_tts(text=text, speaker=voices[role], sample_rate=TTS_RATE).numpy()
        silence = np.zeros_like(speech)
        for name in tracks:
            tracks[name].append(speech if name == role else silence)
            tracks[name].append(gap)
    return {name: np.concatenate(parts) * 0.8 for name, parts in tracks.items()}


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "samples")
    out.mkdir(parents=True, exist_ok=True)
    model, _ = torch.hub.load("snakers4/silero-models", "silero_tts", language="ru", speaker="v4_ru", trust_repo=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # 1. Стерео из двух моно: первый вход -> левый канал (клиент), второй -> правый (оператор)
        tracks = build_tracks(model, DIALOG_STEREO, VOICES["stereo"])
        for name, data in tracks.items():
            write_wav(tmp / f"stereo_{name}.wav", data, TTS_RATE)
        ffmpeg("-i", str(tmp / "stereo_client.wav"), "-i", str(tmp / "stereo_operator.wav"),
               "-filter_complex", "[0:a][1:a]join=inputs=2:channel_layout=stereo:map=0.0-FL|1.0-FR[a]",
               "-map", "[a]", "-c:a", "aac", "-b:a", "96k", str(out / "call_stereo.m4a"))

        # 2. Моно: смешиваем две дорожки
        tracks = build_tracks(model, DIALOG_MONO, VOICES["mono"])
        for name, data in tracks.items():
            write_wav(tmp / f"mono_{name}.wav", data, TTS_RATE)
        ffmpeg("-i", str(tmp / "mono_client.wav"), "-i", str(tmp / "mono_operator.wav"),
               "-filter_complex", "[0:a][1:a]amix=inputs=2:normalize=0[a]", "-map", "[a]",
               "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", str(out / "call_mono.mp3"))

    # 3. Реальная речь (~70 с) из примеров GigaAM (тот же URL, что в gigaam.utils.download_long_audio) -> ogg/opus
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "long_example.wav"
        urllib.request.urlretrieve(LONG_EXAMPLE_URL, src)
        ffmpeg("-i", str(src), "-c:a", "libopus", "-b:a", "32k", str(out / "speech_real.ogg"))

    for f in sorted(out.iterdir()):
        print(f.name, f.stat().st_size)


if __name__ == "__main__":
    main()
