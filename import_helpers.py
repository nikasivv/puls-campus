"""Strict, previewable import of a *fictional* enrolment registry. No external calls."""
import csv
import io
import re

ALIASES = {
    'username': ('username','логин','учетная запись'),
    'full_name': ('full_name','фио','фамилия имя отчество'),
    'group_name': ('group_name','группа','учебная группа'),
}


def parse_registry(raw):
    if not raw or len(raw)>500_000:
        raise ValueError('CSV должен содержать данные и быть меньше 500 КБ.')
    try:
        text=raw.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('Загрузите CSV в кодировке UTF-8.') from exc
    try:
        dialect=csv.Sniffer().sniff(text[:2500],delimiters=',;\t')
    except csv.Error:
        dialect=csv.excel
    reader=csv.DictReader(io.StringIO(text,newline=''),dialect=dialect)
    headers={str(h).strip().lower():h for h in (reader.fieldnames or []) if h}
    mapping={k:next((headers[a] for a in aliases if a in headers),None) for k,aliases in ALIASES.items()}
    if not all(mapping.values()):
        raise ValueError('Нужны столбцы: username, full_name, group_name (или «Логин», «ФИО», «Группа»).')
    rows=[]
    usernames=set()
    for i,raw_row in enumerate(reader,2):
        if i>201:
            raise ValueError('За один раз можно загрузить не более 200 студентов.')
        row={k:(raw_row.get(col) or '').strip() for k,col in mapping.items()}
        row['username']=row['username'].lower()
        errors=[]
        if not re.fullmatch(r'[a-z0-9_.-]{3,32}',row['username']): errors.append('Логин: 3–32 латинских символа')
        if not 3<=len(row['full_name'])<=100: errors.append('Укажите ФИО (3–100 символов)')
        if row['full_name'].lstrip().startswith(('=', '+', '-', '@')):
            errors.append('Недопустимое начало ФИО в CSV')
        if not row['group_name']: errors.append('Нет группы')
        if row['username'] in usernames: errors.append('Логин повторяется в файле')
        usernames.add(row['username'])
        row['line']=i
        row['errors']=errors
        rows.append(row)
    if not rows: raise ValueError('В файле нет студентов.')
    return rows
