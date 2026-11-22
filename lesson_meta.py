"""Human-readable teaching-session metadata, maintained without external dependencies."""
import json
import re

LESSON_TYPES = {
    'ЛК': 'Лекция',
    'ПР': 'Практическое занятие',
    'ЛБ': 'Лабораторная работа',
    'СЕМ': 'Семинар',
    'КОНС': 'Консультация',
    'ЗАЧ': 'Зачёт',
    'ЭКЗ': 'Экзамен',
}


def items_from_text(value, maximum=8, max_length=90):
    """Read names/rooms separated by newlines or semicolons (also comma).

    Do not silently truncate; the staff member must correct invalid input.
    """
    if not isinstance(value, str):
        raise ValueError('Неверный формат списка.')
    items = [part.strip() for part in re.split(r'[;,\n]+', value) if part.strip()]
    items = list(dict.fromkeys(items))
    if not 1 <= len(items) <= maximum or any(len(part) > max_length for part in items):
        raise ValueError(f'Нужен список от 1 до {maximum} элементов (до {max_length} символов каждый).')
    return items


def load_items(value, fallback=''):
    try:
        parsed = json.loads(value or '[]')
        if isinstance(parsed, list) and parsed and all(isinstance(x, str) and x.strip() for x in parsed):
            return parsed
    except (ValueError, TypeError):
        pass
    return [fallback] if fallback else []


def serialize_items(items):
    return json.dumps(items, ensure_ascii=False)


def metadata_from_form(form, defaults=None):
    """Validated type and lists; legacy fields accepted for backwards compatibility."""
    defaults = defaults or {}
    lesson_type = form.get('lesson_type', defaults.get('lesson_type', 'ПР')).strip()
    if lesson_type not in LESSON_TYPES:
        raise ValueError('Выбери корректный тип занятия.')
    teachers = items_from_text(form.get('teachers', form.get('teacher', defaults.get('teacher', ''))), maximum=6, max_length=90)
    rooms = items_from_text(form.get('rooms', form.get('room', defaults.get('room', ''))), maximum=6, max_length=40)
    return dict(lesson_type=lesson_type, teachers=teachers, rooms=rooms,
                teacher=teachers[0], room=rooms[0],
                teachers_json=serialize_items(teachers), rooms_json=serialize_items(rooms))
