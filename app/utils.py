"""Fechas, dinero y textos compartidos."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

BOGOTA = ZoneInfo("America/Bogota")
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
MESES_LARGOS = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

METHODS = {
    "efectivo": "Efectivo",
    "nequi": "Nequi",
    "daviplata": "Daviplata",
    "transferencia": "Transferencia Bancolombia",
    "breb": "Bre-B",
    "tarjeta": "Tarjeta (datáfono)",
}

# Bre-B no es una cuenta: el dinero entra a la cuenta de la llave.
METHOD_ACCOUNT = {
    "efectivo": "caja",
    "nequi": "nequi",
    "daviplata": "daviplata",
    "transferencia": "bancolombia",
    "tarjeta": "datafono",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def as_bogota(value: datetime | date | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time.min, tzinfo=BOGOTA)
    current = as_utc(value)
    return current.astimezone(BOGOTA) if current else None


def today_bogota() -> date:
    return datetime.now(BOGOTA).date()


def period_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    opening = datetime.combine(start, time.min, tzinfo=BOGOTA).astimezone(timezone.utc)
    closing = datetime.combine(end, time.max, tzinfo=BOGOTA).astimezone(timezone.utc)
    return opening, closing


def fmt_date(value: datetime | date | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        current = as_bogota(value)
        if current is None:
            return "—"
        return f"{current.day} {MESES[current.month - 1]} {current.year}"
    return f"{value.day} {MESES[value.month - 1]} {value.year}"


def fmt_dt(value: datetime | None) -> str:
    current = as_bogota(value)
    if current is None:
        return "—"
    return f"{current.day} {MESES[current.month - 1]} {current.year} · {current:%H:%M}"


def fmt_long(value: date) -> str:
    return f"{value.day} de {MESES_LARGOS[value.month - 1]} de {value.year}"


def money(value: int | None) -> str:
    if value is None:
        return "—"
    number = int(value)
    sign = "−" if number < 0 else ""
    body = f"{abs(number):,}".replace(",", ".")
    return f"{sign}${body}"


def fold(value: str | None) -> str:
    raw = unicodedata.normalize("NFD", (value or "").lower())
    return "".join(ch for ch in raw if unicodedata.category(ch) != "Mn")


def parse_money(value: str | None, label: str, *, allow_zero: bool = True, allow_negative: bool = False) -> int:
    raw = (value or "").strip().replace("$", "").replace(" ", "")
    if not raw:
        raise ValueError(f"Falta {label}.")
    negative = raw.startswith("-") or raw.startswith("−")
    if negative:
        raw = raw[1:]
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", raw) or re.fullmatch(r"\d{1,3}(,\d{3})+", raw):
        number = int(raw.replace(".", "").replace(",", ""))
    elif re.fullmatch(r"\d+", raw):
        number = int(raw)
    else:
        raise ValueError(f"{label} debe ser un valor en pesos, sin centavos.")
    if negative:
        if not allow_negative:
            raise ValueError(f"{label} no puede ser negativo.")
        number = -number
    if number == 0 and not allow_zero:
        raise ValueError(f"{label} debe ser mayor que cero.")
    return number


def parse_int(value: str | None, label: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = (value or "").strip()
    if not re.fullmatch(r"-?\d+", raw):
        raise ValueError(f"{label} debe ser un número entero.")
    number = int(raw)
    if minimum is not None and number < minimum:
        raise ValueError(f"{label} no puede ser menor que {minimum}.")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label} no puede ser mayor que {maximum}.")
    return number


def parse_date(value: str | None, label: str) -> date:
    raw = (value or "").strip()
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{label} no es una fecha válida.") from exc


def norm_code(value: str | None, label: str = "El código") -> str:
    code = (value or "").strip().upper()
    if not code:
        raise ValueError(f"{label} es obligatorio.")
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9._\-]{0,39}", code):
        raise ValueError(f"{label} solo admite letras, números, punto o guion.")
    return code


def div_round(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ValueError("No se puede dividir entre cero.")
    if numerator >= 0:
        return (numerator + denominator // 2) // denominator
    return -(((-numerator) + denominator // 2) // denominator)


def slugify(value: str) -> str:
    base = fold(value).replace(" ", "-")
    base = re.sub(r"[^a-z0-9\-]+", "", base).strip("-")
    return base[:40] or "cuenta"
