"""Печать ответа GET /jobs/{id} в читаемом виде (для e2e_api.sh): python show_job.py < ответ.json"""
import json
import sys

d = json.load(sys.stdin)
s, e = d.get("summary"), d.get("emotion")
print(f"  статус={d['status']} длительность={d['duration_s']} с обработка={d['processing_s']} с фраз={len(d['phrases'])}")
print(f"  телефон={d['phone']} внутр={d['ext']} дата={d['call_date']} ошибка={d['error']} предупреждение={d['warning']}")
print(f"  эмоции по голосу: {e['by_channel'] if e else None}; злых фраз: {len(e['top_angry']) if e else '-'}")
print(f"  пример фразы: {next((p for p in d['phrases'] if 'emotion' in p), None)}")
if s:
    print(f"  саммари: тема={s['topic']!r}, тон={s['tone']} ({s['tone_reason']})")
    print(f"  summary: {s['summary']}")
else:
    print("  саммари: НЕТ")
