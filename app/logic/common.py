"""Primitivas: errores, ajustes, cuentas, existencias y autorización."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Account,
    AccountMove,
    AuditLog,
    CashSession,
    Category,
    CostHistory,
    Counter,
    ExpenseCategory,
    Idempotency,
    PriceHistory,
    Setting,
    StockMove,
    User,
    Variant,
)
from app.utils import METHOD_ACCOUNT, fold, utcnow

DEFAULTS = {
    "discount_max_pct": "10",
    "discount_min_total": "150000",
    "layaway_days": "30",
    "layaway_alert_days": "7",
    "layaway_min_pct": "30",
    "breb_account": "bancolombia",
    "business_name": "KINKARA AFRO",
    "business_city": "",
    "business_phone": "",
    "business_nit": "",
    "show_demo_hint": "1",
    "receipt_legend": "Documento no válido como factura",
}

KIND_LABELS = {
    "venta": "Venta entregada",
    "abono_separado": "Abono de separado",
    "abono_fiado": "Abono de fiado",
    "gasto": "Gasto",
    "nomina": "Nómina",
    "abono_proveedor": "Abono a proveedor",
    "devolucion": "Devolución",
    "devolucion_separado": "Devolución de separado",
    "ajuste_apertura": "Diferencia al abrir",
    "ajuste_cierre": "Diferencia al cerrar",
    "traslado": "Traslado",
    "traslado_cierre": "Traslado al cierre",
    "reverso_cierre": "Reverso de cierre",
    "aporte": "Aporte de capital",
    "retiro": "Retiro del dueño",
    "prestamo_entra": "Préstamo recibido",
    "prestamo_sale": "Pago de préstamo",
    "conciliacion": "Conciliación",
    "apertura": "Inventario inicial",
}


class BusinessError(Exception):
    def __init__(self, message: str, redirect: str | None = None):
        super().__init__(message)
        self.message = message
        self.redirect = redirect


class AlreadyDone(Exception):
    def __init__(self, ref_type: str, ref_id: int):
        super().__init__(ref_type)
        self.ref_type = ref_type
        self.ref_id = ref_id


def setting(db: Session, key: str) -> str:
    row = db.get(Setting, key)
    if row is None:
        return DEFAULTS.get(key, "")
    return row.value


def setting_int(db: Session, key: str) -> int:
    raw = setting(db, key).strip()
    return int(raw or 0)


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def audit(db: Session, user: User | None, action: str, entity: str, entity_id: int | None, detail: str = "") -> None:
    db.add(
        AuditLog(
            user_id=user.id if user else None,
            action=action,
            entity=entity,
            entity_id=entity_id,
            detail=detail[:2000],
        )
    )


def next_number(db: Session, name: str, prefix: str) -> str:
    counter = db.get(Counter, name)
    if counter is None:
        counter = Counter(name=name, value=1)
        db.add(counter)
    else:
        counter.value += 1
    db.flush()
    return f"{prefix}-{counter.value:05d}"


def claim(db: Session, key: str, scope: str) -> Idempotency:
    if not key or len(key) > 80:
        raise BusinessError("Recarga la página e inténtalo de nuevo.")
    existing = db.scalar(select(Idempotency).where(Idempotency.key == key))
    if existing and existing.ref_id:
        raise AlreadyDone(existing.ref_type, existing.ref_id)
    if existing:
        raise BusinessError("Esa operación está en curso. Espera un momento y revisa si quedó guardada.")
    row = Idempotency(key=key, scope=scope, ref_type="", ref_id=0)
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise BusinessError(
            "Esa operación acaba de registrarse. Revisa el último comprobante antes de repetirla."
        ) from exc
    return row


def finish(row: Idempotency, ref_type: str, ref_id: int) -> None:
    row.ref_type = ref_type
    row.ref_id = ref_id


def hash_password(password: str) -> str:
    import bcrypt

    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, hashed: str) -> bool:
    import bcrypt

    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == (username or "").strip().lower()))
    if not user or not verify_password(password or "", user.password_hash):
        return None
    return user


def login_user(db: Session, username: str, password: str) -> User:
    user = db.scalar(select(User).where(User.username == (username or "").strip().lower()))
    if not user or not verify_password(password or "", user.password_hash):
        raise BusinessError("Usuario o contraseña incorrectos.")
    if not user.active:
        raise BusinessError("Esta cuenta está desactivada. Pide a un administrador que la reactive.")
    audit(db, user, "inicio_sesion", "user", user.id, user.username)
    return user


def authorize(db: Session, actor: User, username: str, password: str) -> User:
    """Una caja necesita a administración; quien administra confirma con su clave."""
    if actor.is_admin and (not (username or "").strip() or username.strip().lower() == actor.username):
        if not verify_password(password or "", actor.password_hash):
            raise BusinessError("La contraseña de autorización no coincide.")
        return actor
    if (username or "").strip():
        admin = authenticate(db, username, password)
        if not admin or not admin.is_admin or not admin.active:
            raise BusinessError("Se necesita la autorización de un administrador.")
        return admin
    for admin in db.scalars(select(User).where(User.role == "admin", User.active.is_(True))).all():
        if verify_password(password or "", admin.password_hash):
            return admin
    raise BusinessError("Se necesita la autorización de un administrador.")


def count_active_admins(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.active.is_(True))) or 0)


def account_by_code(db: Session, code: str) -> Account:
    account = db.scalar(select(Account).where(Account.code == code, Account.active.is_(True)))
    if account is None:
        account = db.scalar(select(Account).where(Account.code == code))
    if account is None:
        raise BusinessError("Falta una cuenta de dinero del sistema.")
    return account


def account_for_method(db: Session, method: str) -> Account:
    if method == "breb":
        return account_by_code(db, setting(db, "breb_account") or "bancolombia")
    code = METHOD_ACCOUNT.get(method)
    if not code:
        raise BusinessError("Ese medio de pago no está configurado.")
    return account_by_code(db, code)


def find_open_session(db: Session) -> CashSession | None:
    return db.scalar(select(CashSession).where(CashSession.status == "abierta"))


def require_open_session(db: Session) -> CashSession:
    session = find_open_session(db)
    if session is None:
        raise BusinessError("Abre la caja antes de registrar esta operación.", redirect="/caja")
    return session


def apply_account(
    db: Session,
    account: Account,
    amount: int,
    kind: str,
    user: User | None,
    *,
    note: str = "",
    ref_type: str = "",
    ref_id: int = 0,
    cash_session: CashSession | None = None,
    allow_closed_cash: bool = False,
) -> AccountMove | None:
    if amount == 0:
        return None
    if not account.active:
        raise BusinessError(f"La cuenta {account.name} está inactiva.")
    open_session = find_open_session(db)
    session_id = None
    if account.kind == "caja":
        if open_session is None and not allow_closed_cash:
            raise BusinessError("Abre la caja para mover efectivo.", redirect="/caja")
        holder = open_session or cash_session
        session_id = holder.id if holder else None
    elif open_session is not None:
        session_id = open_session.id
    new_balance = account.balance + amount
    if new_balance < 0:
        raise BusinessError(f"{account.name} no tiene saldo suficiente para esta salida.")
    account.balance = new_balance
    move = AccountMove(
        account_id=account.id,
        amount=amount,
        kind=kind,
        ref_type=ref_type,
        ref_id=ref_id,
        user_id=user.id if user else None,
        session_id=session_id,
        note=note,
    )
    db.add(move)
    db.flush()
    return move


def void_move(db: Session, move: AccountMove, reason: str) -> None:
    if move.voided:
        return
    move.voided = True
    move.void_reason = reason[:200]
    move.account.balance -= move.amount
    if move.account.balance < 0:
        raise BusinessError(f"No se puede reversar el movimiento: {move.account.name} quedaría en negativo.")


def apply_stock(
    db: Session,
    variant: Variant,
    bucket: str,
    qty_delta: int,
    reserved_delta: int,
    reason: str,
    unit_cost: int,
    user: User | None,
    *,
    ref_type: str = "",
    ref_id: int = 0,
    note: str = "",
) -> None:
    if bucket == "unidad":
        new_stock = variant.stock_units + qty_delta
        new_reserved = variant.reserved_units + reserved_delta
        if new_stock < 0 or new_reserved < 0 or new_reserved > new_stock:
            raise BusinessError(f"No hay existencias suficientes de {variant.label}.")
        variant.stock_units = new_stock
        variant.reserved_units = new_reserved
    elif bucket == "paquete":
        new_stock = variant.stock_packages + qty_delta
        new_reserved = variant.reserved_packages + reserved_delta
        if new_stock < 0 or new_reserved < 0 or new_reserved > new_stock:
            raise BusinessError(f"No hay paquetes suficientes de {variant.label}.")
        variant.stock_packages = new_stock
        variant.reserved_packages = new_reserved
    elif bucket == "gramo":
        if reserved_delta:
            raise BusinessError("Los gramos sueltos no se reservan. Separa el paquete.")
        new_stock = variant.stock_grams + qty_delta
        if new_stock < 0:
            raise BusinessError(f"No hay gramos suficientes de {variant.label}.")
        variant.stock_grams = new_stock
    else:
        raise BusinessError("Esa unidad de inventario no existe.")
    if qty_delta == 0 and reserved_delta == 0:
        return
    db.add(
        StockMove(
            variant_id=variant.id,
            bucket=bucket,
            qty=qty_delta,
            reserved_delta=reserved_delta,
            reason=reason,
            unit_cost=unit_cost,
            user_id=user.id if user else None,
            ref_type=ref_type,
            ref_id=ref_id,
            note=note,
        )
    )
    db.flush()


def available(variant: Variant, bucket: str) -> int:
    if bucket == "unidad":
        return variant.stock_units - variant.reserved_units
    if bucket == "paquete":
        return variant.stock_packages - variant.reserved_packages
    if bucket == "gramo":
        return variant.stock_grams
    raise BusinessError("Esa unidad de inventario no existe.")


def bucket_cost(variant: Variant, bucket: str) -> int:
    if bucket == "paquete":
        return variant.cost * variant.package_weight_g
    return variant.cost


def bucket_price(variant: Variant, bucket: str) -> int:
    if bucket == "paquete":
        return variant.package_price
    return variant.price


def remember_price(db: Session, variant: Variant, user: User | None) -> None:
    db.add(
        PriceHistory(
            variant_id=variant.id,
            price=variant.price,
            package_price=variant.package_price,
            user_id=user.id if user else None,
        )
    )


def remember_cost(db: Session, variant: Variant, user: User | None, reason: str) -> None:
    db.add(
        CostHistory(
            variant_id=variant.id,
            cost=variant.cost,
            reason=reason[:200],
            user_id=user.id if user else None,
        )
    )


def get_variant(db: Session, variant_id: int) -> Variant:
    variant = db.get(Variant, variant_id)
    if variant is None:
        raise BusinessError("No encontré esa variante.")
    return variant


def category_by_name(db: Session, name: str) -> Category:
    clean = (name or "").strip()
    if not clean:
        raise BusinessError("Elige o escribe una categoría.")
    found = db.scalar(select(Category).where(func.lower(Category.name) == clean.lower()))
    if found:
        return found
    created = Category(name=clean[:80])
    db.add(created)
    db.flush()
    return created


def expense_category(db: Session, slug_or_name: str) -> ExpenseCategory:
    key = (slug_or_name or "").strip()
    found = db.scalar(
        select(ExpenseCategory).where(
            (ExpenseCategory.slug == key) | (func.lower(ExpenseCategory.name) == key.lower())
        )
    )
    if found is None:
        raise BusinessError("Esa categoría de gasto no existe.")
    return found


def due_from_days(days: int) -> datetime:
    return utcnow() + timedelta(days=days)


def search_match(query: str, *parts: str) -> bool:
    needle = fold(query.strip())
    if not needle:
        return True
    haystack = fold(" ".join(part or "" for part in parts))
    return needle in haystack
