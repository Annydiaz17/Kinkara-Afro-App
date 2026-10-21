"""Caja única, cuentas, traslados y movimientos del dueño."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logic.common import (
    BusinessError,
    account_by_code,
    apply_account,
    audit,
    authorize,
    find_open_session,
    next_number,
    void_move,
)
from app.models import Account, AccountMove, CashSession, Reconciliation, User
from app.utils import slugify, utcnow

# El arqueo suma la operación del turno. El ajuste y el traslado del cierre van aparte.
OPERATING_KINDS = {
    "venta",
    "abono_separado",
    "abono_fiado",
    "gasto",
    "nomina",
    "abono_proveedor",
    "devolucion",
    "devolucion_separado",
    "aporte",
    "retiro",
    "prestamo_entra",
    "prestamo_sale",
    "traslado",
    "conciliacion",
}

CLOSE_KINDS = {"ajuste_cierre", "traslado_cierre", "reverso_cierre"}


def open_caja(db: Session, user: User, counted: int, note: str = "") -> CashSession:
    if counted < 0:
        raise BusinessError("El efectivo contado no puede ser negativo.")
    if find_open_session(db):
        raise BusinessError("La caja ya está abierta.")
    caja = account_by_code(db, "caja")
    session = CashSession(
        number=next_number(db, "cash", "CJ"),
        status="abierta",
        opening_base=caja.balance,
        counted_open=counted,
        open_diff=counted - caja.balance,
        open_note=(note or "").strip(),
        opened_by_id=user.id,
        opened_at=utcnow(),
    )
    db.add(session)
    db.flush()
    if session.open_diff:
        apply_account(
            db, caja, session.open_diff, "ajuste_apertura", user,
            note=session.open_note or "Diferencia al abrir",
            ref_type="cash_session", ref_id=session.id,
        )
    audit(db, user, "abrir_caja", "cash_session", session.id, session.number)
    return session


def caja_moves(db: Session, session: CashSession) -> list[AccountMove]:
    caja = account_by_code(db, "caja")
    return list(
        db.scalars(
            select(AccountMove)
            .where(
                AccountMove.account_id == caja.id,
                AccountMove.session_id == session.id,
                AccountMove.voided.is_(False),
            )
            .order_by(AccountMove.id)
        ).all()
    )


def caja_flows(db: Session, session: CashSession) -> dict:
    grouped = {kind: 0 for kind in OPERATING_KINDS}
    for move in caja_moves(db, session):
        if move.kind in grouped:
            grouped[move.kind] += move.amount
    sales = grouped["venta"]
    layaways = grouped["abono_separado"]
    credits = grouped["abono_fiado"]
    other_in = grouped["aporte"] + grouped["prestamo_entra"]
    expenses = -grouped["gasto"]
    payroll = -grouped["nomina"]
    suppliers = -grouped["abono_proveedor"]
    refunds = -(grouped["devolucion"] + grouped["devolucion_separado"])
    owner_out = -(grouped["retiro"] + grouped["prestamo_sale"])
    transfers = -grouped["traslado"]
    # conciliacion puede entrar o salir
    if grouped["conciliacion"] >= 0:
        other_in += grouped["conciliacion"]
    else:
        owner_out += -grouped["conciliacion"]
    incoming = sales + layaways + credits + other_in
    outgoing = expenses + payroll + suppliers + refunds + owner_out + transfers
    expected = session.counted_open + incoming - outgoing
    return {
        "sales": sales,
        "layaways": layaways,
        "credits": credits,
        "other_in": other_in,
        "expenses": expenses,
        "payroll": payroll,
        "suppliers": suppliers,
        "refunds": refunds,
        "owner_out": owner_out,
        "transfers": transfers,
        "incoming": incoming,
        "outgoing": outgoing,
        "expected": expected,
    }


def close_caja(
    db: Session,
    user: User,
    *,
    counted: int,
    transfer_amount: int = 0,
    transfer_account_id: int | None = None,
    note: str = "",
) -> CashSession:
    session = find_open_session(db)
    if session is None:
        raise BusinessError("No hay una caja abierta.")
    if counted < 0:
        raise BusinessError("El efectivo contado no puede ser negativo.")
    if transfer_amount < 0:
        raise BusinessError("El traslado no puede ser negativo.")
    if transfer_amount > counted:
        raise BusinessError("No puedes trasladar más efectivo del que contaste.")
    flows = caja_flows(db, session)
    caja = account_by_code(db, "caja")
    session.expected = flows["expected"]
    session.book_before_close = caja.balance
    session.counted_close = counted
    session.close_diff = counted - flows["expected"]
    session.close_note = (note or "").strip()
    adjust = counted - caja.balance
    if adjust:
        apply_account(
            db, caja, adjust, "ajuste_cierre", user,
            note=session.close_note or "Diferencia de arqueo",
            ref_type="cash_session", ref_id=session.id,
        )
    session.transfer_amount = transfer_amount
    if transfer_amount:
        if not transfer_account_id:
            raise BusinessError("Elige la cuenta que recibe el traslado.")
        target = db.get(Account, transfer_account_id)
        if target is None or not target.active:
            raise BusinessError("La cuenta de destino no está disponible.")
        if target.id == caja.id:
            raise BusinessError("El traslado tiene que salir de la caja hacia otra cuenta.")
        apply_account(
            db, caja, -transfer_amount, "traslado_cierre", user,
            note=f"Traslado al cierre hacia {target.name}",
            ref_type="cash_session", ref_id=session.id,
        )
        apply_account(
            db, target, transfer_amount, "traslado_cierre", user,
            note=f"Traslado al cierre desde caja · {session.number}",
            ref_type="cash_session", ref_id=session.id,
        )
        session.transfer_account_id = target.id
    session.closing_base = caja.balance
    session.status = "cerrada"
    session.closed_by_id = user.id
    session.closed_at = utcnow()
    audit(db, user, "cerrar_caja", "cash_session", session.id, session.number)
    return session


def correct_caja(
    db: Session,
    actor: User,
    session: CashSession,
    *,
    counted: int,
    transfer_amount: int,
    transfer_account_id: int | None,
    note: str,
    username: str,
    password: str,
) -> CashSession:
    authorizer = authorize(db, actor, username, password)
    if session.status != "cerrada":
        raise BusinessError("Solo se corrige un cierre ya confirmado.")
    later = db.scalar(
        select(CashSession).where(CashSession.id > session.id)
    )
    if later is not None or find_open_session(db) is not None:
        raise BusinessError("Solo se corrige el último cierre, y antes de volver a abrir la caja.")
    if counted < 0 or transfer_amount < 0 or transfer_amount > counted:
        raise BusinessError("Revisa el contado y el traslado.")
    caja = account_by_code(db, "caja")
    closing_moves = db.scalars(
        select(AccountMove).where(
            AccountMove.ref_type == "cash_session",
            AccountMove.ref_id == session.id,
            AccountMove.kind.in_(tuple(CLOSE_KINDS)),
            AccountMove.voided.is_(False),
        )
    ).all()
    for move in closing_moves:
        void_move(db, move, "Corrección de cierre")
    flows = caja_flows(db, session)
    session.expected = flows["expected"]
    session.book_before_close = caja.balance
    session.counted_close = counted
    session.close_diff = counted - flows["expected"]
    session.correction_note = (note or "").strip()
    session.corrected = True
    adjust = counted - caja.balance
    if adjust:
        apply_account(
            db, caja, adjust, "ajuste_cierre", authorizer,
            note="Corrección de arqueo", ref_type="cash_session", ref_id=session.id,
            allow_closed_cash=True, cash_session=session,
        )
    session.transfer_amount = transfer_amount
    session.transfer_account_id = None
    if transfer_amount:
        target = db.get(Account, transfer_account_id) if transfer_account_id else None
        if target is None or target.id == caja.id:
            raise BusinessError("Elige la cuenta que recibe el traslado.")
        apply_account(
            db, caja, -transfer_amount, "traslado_cierre", authorizer,
            note=f"Traslado corregido hacia {target.name}",
            ref_type="cash_session", ref_id=session.id,
            allow_closed_cash=True, cash_session=session,
        )
        apply_account(
            db, target, transfer_amount, "traslado_cierre", authorizer,
            note=f"Traslado corregido · {session.number}",
            ref_type="cash_session", ref_id=session.id,
        )
        session.transfer_account_id = target.id
    session.closing_base = caja.balance
    audit(db, authorizer, "corregir_cierre", "cash_session", session.id, note or session.number)
    return session


def transfer(
    db: Session,
    user: User,
    *,
    source_id: int,
    target_id: int,
    amount: int,
    note: str,
) -> None:
    if amount <= 0:
        raise BusinessError("El traslado debe ser mayor que cero.")
    if source_id == target_id:
        raise BusinessError("Elige dos cuentas distintas.")
    source = db.get(Account, source_id)
    target = db.get(Account, target_id)
    if source is None or target is None or not source.active or not target.active:
        raise BusinessError("Revisa las cuentas del traslado.")
    before = _total_money(db)
    apply_account(
        db, source, -amount, "traslado", user,
        note=(note or "").strip() or f"Traslado a {target.name}",
        ref_type="transfer", ref_id=target.id,
    )
    apply_account(
        db, target, amount, "traslado", user,
        note=(note or "").strip() or f"Traslado desde {source.name}",
        ref_type="transfer", ref_id=source.id,
    )
    if _total_money(db) != before:
        raise BusinessError("El traslado no puede cambiar el dinero total del negocio.")
    audit(db, user, "traslado", "account", source.id, f"{source.code} → {target.code} {amount}")


def owner_move(
    db: Session,
    user: User,
    *,
    account_id: int,
    kind: str,
    amount: int,
    note: str,
) -> None:
    if kind not in {"aporte", "retiro", "prestamo_entra", "prestamo_sale"}:
        raise BusinessError("Ese movimiento no corresponde al dueño.")
    if amount <= 0:
        raise BusinessError("El valor debe ser mayor que cero.")
    if not (note or "").strip():
        raise BusinessError("Escribe una observación: de quién es el dinero y por qué.")
    account = db.get(Account, account_id)
    if account is None or not account.active:
        raise BusinessError("Elige una cuenta activa.")
    signed = amount if kind in {"aporte", "prestamo_entra"} else -amount
    apply_account(db, account, signed, kind, user, note=note.strip(), ref_type="owner", ref_id=user.id)
    audit(db, user, kind, "account", account.id, note.strip())


def reconcile(
    db: Session,
    actor: User,
    account: Account,
    *,
    statement_balance: int,
    reason: str,
    username: str,
    password: str,
) -> Reconciliation:
    authorizer = authorize(db, actor, username, password)
    if account.kind == "caja":
        raise BusinessError("La caja se concilia en el arqueo de cierre, no contra un extracto.")
    if statement_balance < 0:
        raise BusinessError("El saldo del extracto no puede ser negativo.")
    if not (reason or "").strip() and statement_balance != account.balance:
        raise BusinessError("Si hay diferencia, escribe el motivo.")
    difference = statement_balance - account.balance
    row = Reconciliation(
        account_id=account.id,
        statement_balance=statement_balance,
        book_balance=account.balance,
        difference=difference,
        reason=(reason or "").strip(),
        user_id=authorizer.id,
    )
    db.add(row)
    db.flush()
    if difference:
        apply_account(
            db, account, difference, "conciliacion", authorizer,
            note=reason.strip(), ref_type="reconciliation", ref_id=row.id,
        )
    audit(db, authorizer, "conciliar", "account", account.id, reason or "sin diferencia")
    return row


def create_account(db: Session, user: User, name: str, kind: str) -> Account:
    if not user.is_admin:
        raise BusinessError("Solo administración crea cuentas.")
    clean = (name or "").strip()
    if len(clean) < 3:
        raise BusinessError("La cuenta necesita un nombre.")
    if kind not in {"banco", "billetera", "otro"}:
        raise BusinessError("Las cuentas de caja, ahorro, Nequi, Daviplata y Bancolombia ya existen.")
    code = slugify(clean)
    base = code
    suffix = 2
    while db.scalar(select(Account).where(Account.code == code)):
        code = f"{base}-{suffix}"
        suffix += 1
    account = Account(code=code, name=clean[:80], kind=kind, balance=0, active=True, system=False)
    db.add(account)
    db.flush()
    audit(db, user, "crear_cuenta", "account", account.id, account.name)
    return account


def _total_money(db: Session) -> int:
    return sum(account.balance for account in db.scalars(select(Account).where(Account.active.is_(True))).all())
