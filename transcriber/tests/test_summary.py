import json
from dataclasses import replace

import pytest

from app.config import Settings
from app.summary import (FACTS_SCHEMA, SIGNALS, SUMMARY_SCHEMA, InvalidAnswer, LLMUnavailable, Summarizer, assess,
                         check_signals, quote_in_transcript, tidy_sentences, tone_from_score, validate)

# вымышленный звонок
TRANSCRIPT = ("[00:01] Вы уже вторую неделю не можете приехать, сколько можно? Почему мы не можем вторую неделю работать?\n"
              "[00:30] Руководитель сказал: «Расторгаем договор и до свидания».\n[01:40] Инженер вам перезвонит.")


def signals(**on):
    """Сигналы: имя=цитата (значение true) для перечисленных, остальные false."""
    return {name: {"quote": on.get(name, ""), "value": name in on} for name in SIGNALS}


FACTS = {
    "caller": {"organization": "ООО Альфа", "person": "Анна", "location": "Тверь"},
    "request": "Вторую неделю не приезжает инженер.",
    "details": ["вторая неделя", "обещали приехать в понедельник"],
    "actions": [],
    "agreements": ["инженер перезвонит"],
    "status": "Не решено",
    "signals": signals(complaint="не можем вторую неделю работать", threat="Расторгаем договор и до свидания"),
}
CARD = {"topic": "Срыв сроков выезда инженера", "problem": "Инженер не приезжает вторую неделю.",
        "result": "Не решено.", "next_step": "Инженер перезвонит.",
        "summary": ["Клиент жалуется на срыв сроков.", "Обещали приехать в понедельник.", "Инженер перезвонит клиенту."]}


def dump(data):
    return json.dumps(data, ensure_ascii=False)


# ---- проверка ответа по схеме

def test_validate_cleans_and_lowercases_enums():
    assert validate(FACTS, FACTS_SCHEMA)["status"] == "не решено"


def test_validate_rejects_chinese_and_bad_types():
    with pytest.raises(InvalidAnswer):
        validate(dict(CARD, topic="客户订购"), SUMMARY_SCHEMA)
    with pytest.raises(InvalidAnswer):
        validate(dict(FACTS, signals=dict(FACTS["signals"], urgent={"quote": "", "value": "может быть"})), FACTS_SCHEMA)


def test_validate_summary_needs_three_sentences():
    with pytest.raises(InvalidAnswer):
        validate(dict(CARD, summary=["Одно.", ""]), SUMMARY_SCHEMA)


# ---- правила оценки

def test_tone_follows_score():
    assert [tone_from_score(x) for x in (0, 1, 2, 5, 6, 10)] == \
        ["позитивный", "позитивный", "нейтральный", "нейтральный", "негативный", "негативный"]


def test_quote_must_be_in_transcript():
    assert quote_in_transcript("не можем вторую неделю работать", TRANSCRIPT)
    assert not quote_in_transcript("вы ужасно работаете, я напишу жалобу", TRANSCRIPT)
    assert not quote_in_transcript("", TRANSCRIPT)


def test_assess_ordinary_call():
    a = assess(signals(), "решено", None)
    assert (a["negative_score"], a["tone"], a["priority"], a["risks"]) == (2, "нейтральный", "обычный", [])


def test_assess_work_stopped_is_important_but_not_negative():
    a = assess(signals(work_stopped="не можем пробить чек"), "не решено", None)
    assert a["priority"] == "важный" and a["tone"] == "нейтральный" and "простой работы у клиента" in a["risks"]


def test_assess_complaint_with_angry_voice():
    a = assess(signals(complaint="вторую неделю"), "не решено", "Эмоции по голосу: злость 29%, грусть 0%")
    assert a["negative_score"] == 7 and a["tone"] == "негативный" and a["priority"] == "важный"
    assert "злость по голосу 29%" in a["tone_reason"]


def test_assess_threat_is_critical():
    a = assess(signals(complaint="вторую неделю", threat="расторгаем договор"), "не решено", None)
    assert a["negative_score"] == 9 and a["priority"] == "критический"
    assert "угроза расторжения договора или ухода клиента" in a["risks"]


def test_assess_warm_thanks_is_positive():
    assert assess(signals(thanks="спасибо огромное, выручили"), "решено", None)["tone"] == "позитивный"


def test_garbage_inside_text_rejected():
    broken = dict(CARD, summary=["Сотрудник подключился.»", "«» «Касса не прошита.»] } 🥛🥛 topic", "Ждём."])
    with pytest.raises(InvalidAnswer):
        validate(broken, SUMMARY_SCHEMA)
    with pytest.raises(InvalidAnswer):
        validate(dict(CARD, summary=["Ошибка 401.', 'Сотрудник подключился.", "Два.", "Три."]), SUMMARY_SCHEMA)


def test_tidy_sentences_splits_dedupes_and_caps():
    items = ["Касса не открывает смену из-за отсутствия лицензии 16. Сотрудник проверил права.",
             "Касса не открывает смену, потому что нет лицензии 16.",
             "Сотрудник перезвонит позже. проблема не решена", "Да.", "Кассу M.4 выключили.", "Клиент ждёт ответа."]
    assert tidy_sentences(items) == [
        "Касса не открывает смену из-за отсутствия лицензии 16.", "Сотрудник проверил права.",
        "Сотрудник перезвонит позже.", "Проблема не решена.", "Кассу M.4 выключили."]


def test_fault_description_is_not_complaint():
    s = signals(complaint="Смску мы отправили, код не приходит")
    check_signals(s, "[00:24] Алло. Смску мы отправили, код не приходит. Сейчас.")
    assert s["complaint"]["value"] is False


def test_polite_thanks_is_not_warm():
    polite, warm = signals(thanks="Спасибо вам большое"), signals(thanks="Огромное спасибо, выручили")
    check_signals(polite, "Спасибо вам большое, до свидания.")
    check_signals(warm, "Огромное спасибо, выручили нас.")
    assert polite["thanks"]["value"] is False and warm["thanks"]["value"] is True


def test_threat_found_by_keywords_when_model_missed_it():
    s = signals()
    check_signals(s, TRANSCRIPT)
    assert s["threat"]["value"] is True and "Расторгаем договор" in s["threat"]["quote"]


# ---- цепочка с подменённой моделью

def make_summarizer(answers):
    s = Summarizer(replace(Settings(), llm_enabled=True))
    calls = []

    def fake_chat(messages, schema, max_tokens):
        calls.append((messages, schema))
        answer = answers[len(calls) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else dump(answer)

    s._chat = fake_chat
    return s, calls


def test_chain_builds_full_card_and_adds_missing_threat():
    s, calls = make_summarizer([FACTS, CARD])
    summary, warning = s.summarize(TRANSCRIPT, voice="Эмоции по голосу: злость 29%")
    assert warning is None
    assert [c[1] for c in calls] == [FACTS_SCHEMA, SUMMARY_SCHEMA]
    assert "Вторую неделю" in calls[1][0][-1]["content"]  # карточка строится по фактам
    assert summary["priority"] == "критический" and summary["tone"] == "негативный"
    # модель не упомянула угрозу в пересказе — код дописал её последним предложением
    assert summary["summary"].endswith("Клиент пригрозил: «Расторгаем договор и до свидания».")


def test_invented_signal_is_dropped():
    invented = dict(FACTS, signals=signals(complaint="вы ужасно работаете, я напишу жалобу"))
    s, _ = make_summarizer([invented, CARD])
    summary, _ = s.summarize("[00:01] Здравствуйте, касса не печатает чеки.\n[00:20] Подключаюсь. Готово, спасибо.")
    assert summary["facts"]["signals"]["complaint"] == {"quote": "", "value": False}
    assert summary["tone"] == "нейтральный" and summary["priority"] == "обычный"


def test_step_retried_once_with_explanation():
    s, calls = make_summarizer(["не json", FACTS, CARD])
    summary, warning = s.summarize(TRANSCRIPT)
    assert summary and warning is None
    assert "не JSON" in calls[1][0][-1]["content"]


def test_failed_facts_gives_null_not_error():
    s, _ = make_summarizer(["мусор", "客户"])
    summary, warning = s.summarize(TRANSCRIPT)
    assert summary is None and "факты" in warning


def test_failed_card_falls_back_to_facts():
    broken = dict(CARD, summary=["Сотрудник подключился.»", "«» «Касса.»] } topic", "Ждём."])
    s, _ = make_summarizer([FACTS, dump(broken), dump(broken)])
    summary, warning = s.summarize(TRANSCRIPT)
    assert "из фактов" in warning
    assert summary["topic"] == "Вторую неделю не приезжает инженер"
    assert summary["next_step"] == "инженер перезвонит"
    assert summary["summary"].startswith("Вторую неделю не приезжает инженер.")
    # оценка считается по фактам и не теряется
    assert summary["priority"] == "критический" and summary["tone"] == "негативный"


def test_llm_unavailable_gives_null():
    s, _ = make_summarizer([LLMUnavailable("ConnectError")])
    summary, warning = s.summarize(TRANSCRIPT)
    assert summary is None and "недоступна" in warning
