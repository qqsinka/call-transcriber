"""Проверка запущенного сервиса (например, после установки в Docker): отправить запись и дождаться результата.

Без зависимостей — только стандартная библиотека Python 3.
Запуск: python smoke_api.py <запись.mp3> [--url http://localhost:8000] [--user transcriber] [--password ...]
"""
import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def request(url: str, auth: str, data: bytes | None = None, content_type: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, {"detail": exc.read().decode(errors="replace")[:300]}


def multipart(fields: dict, file_path: Path) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'
                 f"Content-Type: application/octet-stream\r\n\r\n".encode() + file_path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("audio")
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--user", default="transcriber")
    p.add_argument("--password", default="change-me")
    p.add_argument("--timeout", type=int, default=1200)
    a = p.parse_args()
    auth = f"{a.user}:{a.password}"

    with urllib.request.urlopen(f"{a.url}/health", timeout=10) as resp:
        print("health:", json.dumps(json.load(resp), ensure_ascii=False))
    code, _ = request(f"{a.url}/jobs/nope", "wrong:password")
    print(f"неверный пароль -> HTTP {code} (ожидаем 401)")

    call_id = f"smoke-{int(time.time())}"
    body, ctype = multipart({"call_id": call_id, "phone": "+70000000000"}, Path(a.audio))
    code, resp = request(f"{a.url}/jobs", auth, body, ctype)
    print(f"POST /jobs -> HTTP {code} {resp}")
    if code != 202:
        sys.exit(1)

    started = time.time()
    while time.time() - started < a.timeout:
        _, job = request(f"{a.url}/jobs/{call_id}", auth)
        if job.get("status") in ("done", "error"):
            break
        time.sleep(3)
    took = time.time() - started
    s, e = job.get("summary"), job.get("emotion")
    print(f"статус={job.get('status')} за {took:.0f} с, длительность={job.get('duration_s')} с, "
          f"фраз={len(job.get('phrases') or [])}, ошибка={job.get('error')}, предупреждение={job.get('warning')}")
    print("эмоции по голосу:", e["by_channel"] if e else None)
    if s:
        print(f"саммари: тема={s['topic']!r}, приоритет={s['priority']}, негатив={s['negative_score']}, тон={s['tone']}")
        print("  ", s["summary"])
    sys.exit(0 if job.get("status") == "done" else 1)


if __name__ == "__main__":
    main()
