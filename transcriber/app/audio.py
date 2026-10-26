"""Декодирование любых аудиоформатов через ffmpeg в 16 кГц float32 по каналам."""
import json
import subprocess

import numpy as np

SAMPLE_RATE = 16000


class AudioError(RuntimeError):
    pass


def probe(path: str) -> dict:
    """Количество каналов и длительность первой аудиодорожки."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=channels:format=duration", "-of", "json", path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AudioError(f"ffprobe: {proc.stderr.strip()[:500]}")
    info = json.loads(proc.stdout or "{}")
    streams = info.get("streams") or []
    if not streams:
        raise AudioError("в файле нет аудиодорожки")
    duration = info.get("format", {}).get("duration")
    return {
        "channels": int(streams[0].get("channels") or 1),
        "duration_s": float(duration) if duration not in (None, "N/A") else None,
    }


def decode(path: str, channels: int) -> np.ndarray:
    """Возвращает массив (channels, samples) float32 в [-1, 1], 16 кГц."""
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", "0:a:0",
        "-f", "s16le", "-acodec", "pcm_s16le", "-ac", str(channels), "-ar", str(SAMPLE_RATE), "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise AudioError(f"ffmpeg: {proc.stderr.decode(errors='replace').strip()[:500]}")
    pcm = np.frombuffer(proc.stdout, dtype=np.int16)
    pcm = pcm[: len(pcm) - len(pcm) % channels]
    return (pcm.reshape(-1, channels).T.astype(np.float32) / 32768.0).copy()


def is_fake_stereo(wav: np.ndarray, tol: float = 1e-3) -> bool:
    """Стерео, где оба канала одинаковые (частый случай у АТС) — обрабатываем как моно."""
    if wav.shape[0] != 2 or wav.shape[1] == 0:
        return False
    return float(np.mean(np.abs(wav[0] - wav[1]))) < tol


def load_channels(path: str, stereo_map: tuple[str, str]) -> tuple[list[tuple[str, np.ndarray]], float]:
    """[(имя_канала, сигнал)], длительность. Стерео -> два канала по STEREO_MAP, иначе 'mono'."""
    info = probe(path)
    if info["channels"] == 2:
        wav = decode(path, 2)
        if not is_fake_stereo(wav):
            duration = wav.shape[1] / SAMPLE_RATE
            return [(stereo_map[0], wav[0]), (stereo_map[1], wav[1])], duration
        mono = wav[0]
    else:
        mono = decode(path, 1)[0]
    return [("mono", mono)], mono.shape[0] / SAMPLE_RATE
