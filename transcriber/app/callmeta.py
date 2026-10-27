"""Метаданные звонка из имени файла записи АТС.

Формат: ГГ-ММ-ДД_ЧЧ-ММ-СС_<кто звонит>_<куда звонят>_<id звонка>.mp3
  26-10-26_09-02-47_79036444845_306090_1792994567.120720.mp3  — входящий: клиент 79036444845 на линию 306090
  26-10-20_17-46-44_103_89155315434_1792507604.110735.mp3     — исходящий: внутренний 103 звонит клиенту
"""
import re

_NAME = re.compile(r"^(\d\d)-(\d\d)-(\d\d)_(\d\d)-(\d\d)-(\d\d)_([^_]+)_([^_]+)_(.+)$")
INTERNAL_MAX_DIGITS = 4  # внутренние номера короткие (101, 208, 230), внешние — 6+ цифр


def is_internal(number: str) -> bool:
    return number.isdigit() and len(number) <= INTERNAL_MAX_DIGITS


def parse_call_name(stem: str) -> dict:
    """{call_date, phone, ext, direction} или {} если имя не в формате АТС."""
    m = _NAME.match(stem)
    if not m:
        return {}
    yy, mo, dd, hh, mi, ss, src, dst, _uid = m.groups()
    if is_internal(src) and not is_internal(dst):
        direction, phone, ext = "outgoing", dst, src
    elif is_internal(src) and is_internal(dst):
        direction, phone, ext = "internal", src, dst
    else:
        direction, phone, ext = "incoming", src, dst
    return {
        "call_date": f"20{yy}-{mo}-{dd}T{hh}:{mi}:{ss}",
        "phone": phone,
        "ext": ext,
        "direction": direction,
    }
