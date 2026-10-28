"""Саммари звонка локальной LLM через Ollama: факты и сигналы (LLM) -> оценка (правила) -> карточка (LLM).

Промпты — в prompts.py. Ошибки LLM никогда не роняют задачу: нет фактов или карточки -> summary=null
с предупреждением.
"""
import json
import logging
import re
import time

import httpx

from . import prompts
from .config import Settings

log = logging.getLogger(__name__)

STATUSES = ("решено", "частично решено", "не решено", "неясно")


def _str(max_len: int) -> dict:
    return {"type": "string", "maxLength": max_len}


def _list(max_items: int, item_len: int = 200, min_items: int = 0) -> dict:
    return {"type": "array", "items": _str(item_len), "minItems": min_items, "maxItems": max_items}


def _object(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props)}


SIGNALS = {
    "complaint": "претензия к сервису",
    "threat": "угроза расторжения/ухода/жалобы",
    "repeat": "повторное обращение",
    "work_stopped": "работа клиента остановлена",
    "urgent": "клиент торопит",
    "thanks": "клиент благодарит",
}

# Схемы задают не только формат, но и длину: Ollama ограничивает генерацию грамматикой,
# промптом длину маленькая модель не держит (было 4-12 предложений вместо 3-5).
# В сигнале сначала цитата, потом значение: порядок полей = порядок генерации.
FACTS_SCHEMA = _object({
    "caller": _object({"organization": _str(120), "person": _str(80), "location": _str(150)}),
    "request": _str(400),
    "details": _list(10),
    "actions": _list(6),
    "agreements": _list(5),
    "status": {"type": "string", "enum": list(STATUSES)},
    "signals": _object({name: _object({"quote": _str(200), "value": {"type": "boolean"}}) for name in SIGNALS}),
})
SUMMARY_SCHEMA = _object({
    "topic": _str(60),
    "problem": _str(400),
    "result": _str(300),
    "next_step": _str(300),
    "summary": _list(5, 220, min_items=3),
})

# Qwen и др. иногда «съезжают» в китайский: такой ответ считаем невалидным
_CJK = re.compile(r"[　-〿぀-ヿ㐀-䶿一-鿿가-힯＀-￯]")
# Модель иногда «ломает» структуру и пишет внутрь строки обрывки JSON, имена полей и эмодзи
_GARBAGE = re.compile(r"[\U0001F000-\U0001FAFF☀-➿]|\]\s*\}|«\s*»|\"\s*,\s*\"|'\s*,\s*'|"
                      r"\b(topic|summary|problem|next_step|result|signals)\b")

# Претензия к сервису — это упрёк компании или её сотрудникам, а не описание неисправности: в цитате должно быть
# обращение к компании или упрёк по времени («вы…», «опять», «обещали», «третью неделю», «никто не перезвонил»)
_COMPLAINT_MARKERS = re.compile(
    r"\b(вы|вас|вам|ваш\w*|опять|снова|обещал\w*|договарива\w*|недел\w*|сколько можно|до сих пор|никто|"
    r"не перезвон\w*|не позвонил\w*|испытыва\w*|безобраз\w*|невозможно|издева\w*)\b", re.IGNORECASE)
# Тёплая благодарность, а не вежливое «спасибо, до свидания»
_WARM_THANKS = re.compile(r"огромн|выручил|молодц|очень помог|отличн|супер|здорово|благодар|золот", re.IGNORECASE)
# Резервная проверка угроз по ключевым словам: модель может пропустить угрозу, пересказанную со слов директора
_THREAT_WORDS = re.compile(r"[^.!?\n]*(расторг|разорв\w* договор|уйд[её]м к|откаж\w* от ваших|жалоб\w* (руководств|директор)|"
                           r"напиш\w* жалоб|в суд)[^.!?\n]*", re.IGNORECASE)


class LLMUnavailable(RuntimeError):
    pass


class InvalidAnswer(ValueError):
    pass


def validate(value, schema: dict, path: str = "ответ"):
    """Проверка ответа модели по схеме (типы, enum, без CJK). Возвращает очищенное значение."""
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict):
            raise InvalidAnswer(f"{path}: ожидался объект")
        return {key: validate(value.get(key), sub, f"{path}.{key}") for key, sub in schema["properties"].items()}
    if kind == "array":
        if value is None:
            value = []
        if isinstance(value, str):
            value = [value] if value.strip() else []
        if not isinstance(value, list):
            raise InvalidAnswer(f"{path}: ожидался массив")
        items = [validate(x, schema["items"], f"{path}[]") for x in value]
        items = [x for x in items if x]
        if len(items) < schema.get("minItems", 0):
            raise InvalidAnswer(f"{path}: нужно не меньше {schema['minItems']} элементов")
        return items[: schema.get("maxItems", len(items))]
    if kind == "boolean":
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            value = value.strip().lower() == "true"
        if not isinstance(value, bool):
            raise InvalidAnswer(f"{path}: ожидалось true/false")
        return value
    if kind == "integer":
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value)
        if not isinstance(value, int) or isinstance(value, bool):
            raise InvalidAnswer(f"{path}: ожидалось число")
        return max(schema.get("minimum", value), min(schema.get("maximum", value), value))
    # string
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise InvalidAnswer(f"{path}: ожидалась строка")
    if _CJK.search(value):
        raise InvalidAnswer(f"{path}: текст не на русском")
    garbage = _GARBAGE.search(value)
    if garbage:
        # сработавший кусок с соседними символами — по логу видно, мусор это или слишком строгая проверка
        near = value[max(0, garbage.start() - 15):garbage.end() + 15].replace("\n", " ")
        raise InvalidAnswer(f"{path}: в тексте обрывки JSON или посторонние символы: «{near}»")
    value = value.strip()
    if "enum" in schema:
        value = value.lower()
        if value not in schema["enum"]:
            raise InvalidAnswer(f"{path}: допустимо только {', '.join(schema['enum'])}")
    return value


def parse_json(raw: str):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise InvalidAnswer("не JSON") from exc


def tone_from_score(score: int) -> str:
    """Тон выводим из оценки по правилу — так тон и балл никогда не противоречат друг другу."""
    if score >= 6:
        return "негативный"
    if score <= 1:
        return "позитивный"
    return "нейтральный"


_WORD = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)


def quote_in_transcript(quote: str, transcript: str, min_share: float = 0.6) -> bool:
    """Цитата действительно есть в расшифровке: не меньше 60% её значимых слов встречаются в тексте.
    Защита от выдуманных «доказательств» (модель может сочинить претензию, которой не было)."""
    words = [w.lower() for w in _WORD.findall(quote) if len(w) >= 3]
    if not words:
        return False
    text = {w.lower() for w in _WORD.findall(transcript)}
    return sum(w in text for w in words) / len(words) >= min_share


def tidy_sentences(items: list[str], limit: int = 5) -> list[str]:
    """Пересказ -> не больше limit разных предложений.
    Модель иногда пишет в один элемент массива несколько предложений и повторяет одно и то же другими словами."""
    sentences, seen = [], []
    for item in items:
        # граница предложения — точка после слова из 2+ символов: сокращения вроде «Касса M. и …» не режем
        for sentence in re.split(r"(?<=\w\w[.!?…])\s+", item.strip()):
            sentence = sentence.strip()
            # сравниваем по основам (первые 5 букв), чтобы «лицензии»/«лицензия» считались одним словом
            words = {w.lower()[:5] for w in _WORD.findall(sentence) if len(w) >= 3}
            if len(words) < 2:
                continue
            # почти-повтор: половина основ уже встречалась в одном из предыдущих предложений
            if any(len(words & prev) / len(words) >= 0.5 for prev in seen):
                continue
            seen.append(words)
            sentence = sentence[0].upper() + sentence[1:]
            sentences.append(sentence if sentence[-1] in ".!?…" else sentence + ".")
    return sentences[:limit]


def card_from_facts(facts: dict) -> dict:
    """Запасная карточка, если LLM дважды не смогла написать свою: собираем из фактов первого шага.
    Пересказ суше, но звонок не остаётся без карточки, а приоритет, тон и риски (считаются по фактам) не теряются."""
    request = facts["request"].strip().rstrip(".") or "Обращение клиента"
    first = re.split(r"[.,:;!?—]", request, maxsplit=1)[0].split()
    return {
        "topic": " ".join(first[:7])[:60],
        "problem": request[:400],
        "result": "; ".join(facts["agreements"] or facts["actions"])[:300] or f"Статус: {facts['status']}",
        "next_step": "; ".join(facts["agreements"])[:300],
        # tidy_sentences в summarize() доведёт до предложений с заглавной буквы и точкой
        "summary": [request, *facts["actions"][:2], *facts["agreements"][:2]],
    }


def check_signals(signals: dict, transcript: str) -> None:
    """Проверка сигналов модели правилами (на месте). Каждое отклонение пишется в лог."""
    for name, signal in signals.items():
        if not signal["value"]:
            continue
        reason = None
        if not quote_in_transcript(signal["quote"], transcript):
            reason = "цитаты нет в расшифровке"
        elif name == "complaint" and not _COMPLAINT_MARKERS.search(signal["quote"]):
            reason = "это описание неисправности, а не упрёк сервису"
        elif name == "thanks" and not _WARM_THANKS.search(signal["quote"]):
            reason = "обычная вежливая благодарность"
        if reason:
            log.info("Сигнал %s отброшен (%s): «%s»", name, reason, signal["quote"])
            signal.update(value=False, quote="")
    if not signals["threat"]["value"]:
        match = _THREAT_WORDS.search(transcript)
        if match:
            quote = re.sub(r"^\[\d\d:\d\d\]\s*", "", match.group(0).strip(" «»\"—-"))
            log.info("Угроза найдена по ключевым словам: «%s»", quote)
            signals["threat"].update(value=True, quote=quote)


def voice_anger(voice: str | None) -> float:
    """Доля злости из текстовой сводки эмоций по голосу («злость 29%»), максимум по каналам."""
    shares = [int(x) for x in re.findall(r"злость (\d+)%", voice or "")]
    return max(shares, default=0) / 100


def assess(signals: dict, status: str, voice: str | None) -> dict:
    """Оценка звонка правилами по проверенным сигналам — прозрачно и воспроизводимо.

    Негатив 0-10: 2 — обычный деловой звонок; 4 — клиент торопит; 6 — претензия к сервису (+1 повторное
    обращение, +1 злость по голосу от 20%); 8-9 — угроза расторжения/ухода; 1 — тёплая благодарность без претензий.
    Приоритет: критический — угроза или негатив >= 8; важный — работа клиента стоит, негатив >= 6
    или повторное обращение по нерешённой проблеме; иначе обычный.
    """
    on = {name: s["value"] for name, s in signals.items()}
    angry_voice = voice_anger(voice) >= 0.2
    score = 2
    if on["urgent"]:
        score = 4
    if on["complaint"]:
        score = 6 + on["repeat"] + angry_voice
    if on["threat"]:
        score = max(score, 8) + on["complaint"]
    if on["thanks"] and not (on["complaint"] or on["threat"]):
        score = 1
    score = max(0, min(10, score))

    if on["threat"] or score >= 8:
        priority = "критический"
    elif on["work_stopped"] or score >= 6 or (on["repeat"] and status != "решено"):
        priority = "важный"
    else:
        priority = "обычный"

    reasons = [f"{SIGNALS[name]}: «{signals[name]['quote']}»" for name in SIGNALS if on[name]]
    if angry_voice:
        reasons.append(f"злость по голосу {voice_anger(voice):.0%}")
    risks = []
    if on["threat"]:
        risks.append("угроза расторжения договора или ухода клиента")
    if on["complaint"]:
        risks.append("недовольство клиента работой сервиса")
    if on["repeat"] and status != "решено":
        risks.append("повторное обращение по нерешённой проблеме")
    if on["work_stopped"] and status != "решено":
        risks.append("простой работы у клиента")
    reason = "; ".join(reasons) or "деловой разговор без претензий"
    return {"negative_score": score, "tone": tone_from_score(score), "tone_reason": reason,
            "priority": priority, "priority_reason": reason, "risks": risks}


def clip_transcript(text: str, max_chars: int) -> str:
    """Слишком длинный разговор: оставляем начало и конец (там тема и итог)."""
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.6)
    tail = max_chars - head
    return text[:head] + "\n[...часть разговора пропущена...]\n" + text[-tail:]


def compact(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1)


class Summarizer:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _chat(self, messages: list[dict], schema: dict, max_tokens: int) -> str:
        s = self.settings
        payload = {
            "model": s.llm_model,
            "messages": messages,
            "stream": False,
            "format": schema,
            # num_predict: под JSON-схемой модель иногда зацикливается (видели 4400+ токенов за 10 мин) — обрываем,
            # обрезанный JSON не пройдёт проверку и уйдёт в повтор
            "options": {"temperature": 0.1, "num_ctx": 8192, "num_predict": min(max_tokens, s.llm_max_tokens)},
            "keep_alive": s.llm_keep_alive,
        }
        try:
            resp = httpx.post(f"{s.llm_url}/api/chat", json=payload, timeout=s.llm_timeout_s)
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code != 200:
            raise LLMUnavailable(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json().get("message", {}).get("content", "")

    def _step(self, name: str, system: str, user: str, schema: dict, max_tokens: int) -> dict:
        """Один шаг цепочки: запрос, проверка по схеме, при ошибке — один повтор с объяснением."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        problem = ""
        for attempt in (1, 2):
            raw = self._chat(messages, schema, max_tokens)
            try:
                return validate(parse_json(raw), schema)
            except InvalidAnswer as exc:
                problem = str(exc)
                log.warning("LLM, шаг %s, попытка %d: %s", name, attempt, problem)
                log.debug("Ответ LLM целиком: %s", raw)
                messages += [{"role": "assistant", "content": raw},
                             {"role": "user", "content": prompts.RETRY_HINT.format(problem=problem)}]
        raise InvalidAnswer(f"шаг «{name}»: {problem}")

    def summarize(self, transcript: str, voice: str | None = None) -> tuple[dict | None, str | None]:
        """(summary | None, предупреждение | None). voice — текстовая сводка эмоций по голосу, если есть."""
        if not self.settings.llm_enabled:
            return None, "LLM отключена (LLM_ENABLED=false)"
        if not transcript.strip():
            return None, "пустой транскрипт, саммари не строилось"
        timing = {}
        try:
            started = time.time()
            facts = self._step("факты", prompts.FACTS_SYSTEM,
                               prompts.FACTS_USER.format(transcript=clip_transcript(transcript, self.settings.llm_max_chars)),
                               FACTS_SCHEMA, 1400)
            timing["facts"] = round(time.time() - started, 1)

            check_signals(facts["signals"], transcript)
            assessment = assess(facts["signals"], facts["status"], voice)

            started = time.time()
            warning = None
            try:
                card = self._step("карточка", prompts.SUMMARY_SYSTEM, prompts.SUMMARY_USER.format(facts=compact(facts)),
                                  SUMMARY_SCHEMA, 700)
            except InvalidAnswer as exc:
                log.warning("Карточка собрана из фактов без LLM: %s", exc)
                card, warning = card_from_facts(facts), f"карточка собрана из фактов без LLM ({exc})"
            timing["summary"] = round(time.time() - started, 1)
        except LLMUnavailable as exc:
            log.warning("LLM недоступна: %s", exc)
            return None, f"LLM недоступна: {exc}"
        except InvalidAnswer as exc:
            return None, f"LLM вернула невалидный ответ ({exc})"

        sentences = tidy_sentences(card["summary"]) or card["summary"]
        threat = facts["signals"]["threat"]
        # угроза — главное в таком звонке: если модель не упомянула её в пересказе, дописываем сами
        if threat["value"] and not re.search(r"расторг|угрож|угроз|уйд|уход|жалоб", " ".join(sentences), re.IGNORECASE):
            sentences = sentences[:4] + [f"Клиент пригрозил: «{threat['quote']}»."]
        return {**card, "summary": " ".join(sentences), **assessment, "facts": facts, "llm_seconds": timing}, warning

    def warmup(self) -> None:
        """Загрузить модель в память без генерации (пустой prompt в /api/generate)."""
        s = self.settings
        try:
            httpx.post(f"{s.llm_url}/api/generate", json={"model": s.llm_model, "prompt": "", "keep_alive": s.llm_keep_alive},
                       timeout=s.llm_timeout_s)
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc

    def health(self) -> dict:
        if not self.settings.llm_enabled:
            return {"enabled": False}
        try:
            resp = httpx.get(f"{self.settings.llm_url}/api/tags", timeout=3)
            models = {m.get("name") for m in resp.json().get("models", [])}
            wanted = self.settings.llm_model
            available = wanted in models or f"{wanted}:latest" in models
            return {"enabled": True, "reachable": True, "model": wanted, "model_available": available}
        except Exception as exc:
            return {"enabled": True, "reachable": False, "error": f"{type(exc).__name__}: {exc}"}
