import os
import time

from app.replacements import EXAMPLE, Replacer, parse_rules, apply_rules


def rules_from_example():
    return parse_rules(EXAMPLE.read_text(encoding="utf-8").splitlines(), "example")


def test_example_rules_compile():
    lines = [x for x in EXAMPLE.read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")]
    assert len(rules_from_example()) == len(lines)


def test_typical_asr_errors():
    rules = rules_from_example()
    fix = lambda s: apply_rules(s, rules)  # noqa: E731
    assert fix("Для нового магазина нужен ключ ФСР.") == "Для нового магазина нужен ключ ФСРАР."
    assert fix("Поставьте Standarbird или Outlook.") == "Поставьте Thunderbird или Outlook."
    assert fix("Разторгаем договор.") == "Расторгаем договор."
    assert fix("Проверьте адрес в фиасе.") == "Проверьте адрес в ФИАС."
    assert fix("У вас же майл почта?") == "У вас же Mail почта?"


def test_terms_and_word_boundaries():
    rules = rules_from_example()
    fix = lambda s: apply_rules(s, rules)  # noqa: E731
    assert fix("ключ ФСРАР уже есть") == "ключ ФСРАР уже есть"  # не превращаем в ФСРАРАР
    assert fix("касса Отол не печатает") == "касса АТОЛ не печатает"
    assert fix("ошибка в егаис") == "ошибка в ЕГАИС"
    assert fix("проверка по честному знаку") == "проверка по Честному ЗНАКу"
    assert fix("зайдите в энидеск") == "зайдите в AnyDesk"
    assert fix("у меня 1с не включается") == "у меня 1С не включается"
    assert fix("майлз") == "майлз"  # только целые слова


def test_bad_lines_skipped():
    rules = parse_rules(["# комментарий", "", "без разделителя", "([ => x", r"\bкот\b => пёс"])
    assert len(rules) == 1
    assert apply_rules("Кот и котик", rules) == "пёс и котик"


def test_file_reload(tmp_path):
    path = tmp_path / "replacements.txt"
    path.write_text(r"\bкот\b => пёс" + "\n", encoding="utf-8")
    replace = Replacer(path)
    assert replace("кот") == "пёс"
    path.write_text(r"\bкот\b => кошка" + "\n", encoding="utf-8")
    later = time.time() + 5
    os.utime(path, (later, later))
    assert replace("кот") == "кошка"


def test_copies_example_when_missing(tmp_path):
    path = tmp_path / "data" / "replacements.txt"
    Replacer(path)
    assert path.read_text(encoding="utf-8") == EXAMPLE.read_text(encoding="utf-8")
