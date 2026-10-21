"""Ventas, fiados, separados, devoluciones y clientas."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.logic.common import (
    AlreadyDone,
    BusinessError,
    account_by_code,
    account_for_method,
    apply_account,
    apply_stock,
    audit,
    authorize,
    available,
    bucket_cost,
    bucket_price,
    claim,
    find_open_session,
    finish,
    get_variant,
    require_open_session,
    setting_int,
)
from app.models import Customer, Layaway, Payment, Sale, SaleLine, User, Variant
from app.utils import METHOD_ACCOUNT, as_bogota, fold, today_bogota, utcnow


def empty_cart() -> dict:
    return {
        "lines": [],
        "discount_pct": 0,
        "customer_id": None,
        "payments": [],
        "due_date": "",
        "note": "",
        "idempotency_key": uuid.uuid4().hex,
    }


def save_customer(
    db: Session,
    user: User,
    *,
    name: str,
    phone: str = "",
    doc_id: str = "",
    email: str = "",
    notes: str = "",
    consent: bool,
    customer_id: int | None = None,
) -> Customer:
    clean_name = " ".join((name or "").split())
    if len(clean_name) < 3:
        raise BusinessError("Escribe el nombre de la clienta.")
    phone = "".join(ch for ch in (phone or "") if ch.isdigit())
    doc_id = (doc_id or "").strip()
    if not consent and customer_id is None:
        raise BusinessError("Falta la autorización para tratar los datos (Ley 1581 de 2012).")
    others = db.scalars(select(Customer).where(Customer.id != (customer_id or 0))).all()
    for other in others:
        if phone and other.phone == phone:
            raise BusinessError(f"Ya existe una clienta con el teléfono {phone}: {other.name}.")
        if doc_id and other.doc_id == doc_id:
            raise BusinessError(f"Ya existe una clienta con el documento {doc_id}: {other.name}.")
        if not phone and not doc_id and fold(other.name) == fold(clean_name):
            raise BusinessError("Ya existe una clienta con ese nombre. Agrégale el teléfono para distinguirla.")
    if customer_id:
        customer = db.get(Customer, customer_id)
        if customer is None:
            raise BusinessError("No encontré esa clienta.")
        customer.name = clean_name[:160]
        customer.phone = phone
        customer.doc_id = doc_id
        customer.email = (email or "").strip()[:120]
        customer.notes = (notes or "").strip()
        if consent and not customer.consent:
            customer.consent = True
            customer.consent_at = utcnow()
            customer.consent_user_id = user.id
        audit(db, user, "editar_cliente", "customer", customer.id, customer.name)
        return customer
    customer = Customer(
        name=clean_name[:160],
        phone=phone,
        doc_id=doc_id,
        email=(email or "").strip()[:120],
        notes=(notes or "").strip(),
        consent=True,
        consent_at=utcnow(),
        consent_user_id=user.id,
    )
    db.add(customer)
    db.flush()
    audit(db, user, "crear_cliente", "customer", customer.id, customer.name)
    return customer


def require_consent(customer: Customer) -> None:
    if not customer.consent:
        raise BusinessError(
            f"{customer.name} todavía no autoriza el tratamiento de sus datos. Actualiza su ficha antes de fiar o separar."
        )


def _line_snapshot(db: Session, item: dict) -> dict:
    variant = get_variant(db, int(item["variant_id"]))
    if not variant.active or not variant.product.active:
        raise BusinessError(f"{variant.label} está inactiva.")
    bucket = item["bucket"]
    qty = int(item["qty"])
    if qty <= 0:
        raise BusinessError("La cantidad debe ser mayor que cero.")
    if bucket not in {"unidad", "paquete", "gramo"}:
        raise BusinessError("Elige unidad, paquete o gramos.")
    if variant.unit == "unidad" and bucket != "unidad":
        raise BusinessError(f"{variant.label} se vende por unidad.")
    if variant.unit == "gramo" and bucket == "unidad":
        raise BusinessError(f"{variant.label} se vende por paquete o por gramos.")
    price = bucket_price(variant, bucket)
    seen = item.get("price_seen", price)
    if int(seen) != price:
        raise BusinessError(f"El precio de {variant.label} cambió. Quítala del carrito y agrégala de nuevo.")
    names = {"unidad": "unidades", "paquete": "paquetes", "gramo": "gramos"}
    if available(variant, bucket) < qty:
        raise BusinessError(
            f"No hay {names[bucket]} suficientes de {variant.label}. Disponible: {available(variant, bucket)}."
        )
    return {
        "variant": variant,
        "bucket": bucket,
        "qty": qty,
        "unit_price": price,
        "unit_cost": bucket_cost(variant, bucket),
        "subtotal": price * qty,
    }


def quote_cart(db: Session, cart: dict) -> dict:
    """Calcula el carrito sin escribir. Los errores de precio o existencia quedan en `problems`."""
    lines = []
    problems = []
    for item in cart.get("lines") or []:
        try:
            lines.append(_line_snapshot(db, item))
        except BusinessError as exc:
            problems.append(exc.message)
    subtotal = sum(line["subtotal"] for line in lines)
    pct = int(cart.get("discount_pct") or 0)
    discount = (subtotal * pct + 50) // 100 if pct > 0 and subtotal else 0
    if discount > subtotal:
        discount = subtotal
    # Reparte el descuento para que la suma de las líneas cierre con el total.
    remaining = discount
    for index, line in enumerate(lines):
        if subtotal == 0 or discount == 0:
            share = 0
        elif index == len(lines) - 1:
            share = remaining
        else:
            share = (line["subtotal"] * discount) // subtotal
            remaining -= share
        line["discount_share"] = share
        line["line_total"] = line["subtotal"] - share
    total = subtotal - discount
    payments = cart.get("payments") or []
    declared = sum(int(pay["amount"]) for pay in payments)
    confirmed = sum(int(pay["amount"]) for pay in payments if pay.get("confirmed", True))
    balance = total - confirmed
    if declared > total:
        problems.append("Los pagos superan el total.")
    return {
        "lines": lines,
        "problems": problems,
        "subtotal": subtotal,
        "discount_pct": pct,
        "discount": discount,
        "total": total,
        "declared": declared,
        "confirmed": confirmed,
        "balance": balance,
        "unconfirmed": declared - confirmed,
    }


def check_discount(db: Session, actor: User, subtotal: int, pct: int, username: str, password: str) -> int | None:
    if pct <= 0:
        return None
    if pct > 100:
        raise BusinessError("El descuento no puede pasar del 100%.")
    max_pct = setting_int(db, "discount_max_pct")
    minimum = setting_int(db, "discount_min_total")
    if actor.is_admin or (pct <= max_pct and subtotal >= minimum):
        return actor.id if actor.is_admin and pct > max_pct else None
    authorizer = authorize(db, actor, username, password)
    audit(db, authorizer, "autorizar_descuento", "sale", None, f"{pct}% sobre {subtotal}")
    return authorizer.id


def put_line(cart: dict, variant: Variant, bucket: str, qty: int) -> None:
    price = bucket_price(variant, bucket)
    for line in cart["lines"]:
        if line["variant_id"] == variant.id and line["bucket"] == bucket and line.get("price_seen") == price:
            line["qty"] += qty
            return
    cart["lines"].append({
        "variant_id": variant.id,
        "bucket": bucket,
        "qty": qty,
        "price_seen": price,
    })


def put_payment(cart: dict, method: str, amount: int, confirmed: bool = True) -> None:
    if amount <= 0:
        raise BusinessError("El pago debe ser mayor que cero.")
    cart["payments"].append({"method": method, "amount": int(amount), "confirmed": confirmed})


def confirm_sale(
    db: Session,
    actor: User,
    cart: dict,
    *,
    admin_username: str = "",
    admin_password: str = "",
    new_customer: dict | None = None,
) -> Sale:
    token = claim(db, cart.get("idempotency_key") or "", "sale")
    session = require_open_session(db)
    if new_customer and (new_customer.get("name") or "").strip():
        customer = save_customer(db, actor, customer_id=None, **new_customer)
        cart["customer_id"] = customer.id
    quote = quote_cart(db, cart)
    if quote["problems"]:
        raise BusinessError(quote["problems"][0])
    if not quote["lines"]:
        raise BusinessError("El carrito está vacío.")
    auth_id = check_discount(
        db, actor, quote["subtotal"], quote["discount_pct"], admin_username, admin_password
    )
    total = quote["total"]
    payments = list(cart.get("payments") or [])
    declared = sum(int(pay["amount"]) for pay in payments)
    if declared > total:
        raise BusinessError("La suma de los pagos supera el total.")
    balance = total - sum(int(pay["amount"]) for pay in payments if pay.get("confirmed", True))
    customer = None
    if cart.get("customer_id"):
        customer = db.get(Customer, int(cart["customer_id"]))
        if customer is None or not customer.active:
            raise BusinessError("No encontré esa clienta.")
    if balance > 0:
        if customer is None:
            raise BusinessError("Para dejar saldo fiado elige o registra una clienta.")
        require_consent(customer)
    if any(not pay.get("confirmed", True) for pay in payments) and customer is None and balance > 0:
        raise BusinessError("Un pago sin confirmar deja saldo. Asocia una clienta.")
    due = None
    if balance > 0:
        raw_due = (cart.get("due_date") or "").strip()
        if raw_due:
            due = date.fromisoformat(raw_due)
        else:
            due = today_bogota() + timedelta(days=30)
    sale = Sale(
        number=next_sale_number(db),
        customer_id=customer.id if customer else None,
        user_id=actor.id,
        session_id=session.id,
        source="pos",
        subtotal=quote["subtotal"],
        discount_pct=quote["discount_pct"],
        discount_amount=quote["discount"],
        discount_auth_id=auth_id,
        total=total,
        paid=0,
        balance=total,
        due_date=due,
        status="confirmada",
        note=(cart.get("note") or "").strip(),
    )
    db.add(sale)
    db.flush()
    for line in quote["lines"]:
        variant = line["variant"]
        apply_stock(
            db, variant, line["bucket"], -line["qty"], 0, "venta", line["unit_cost"], actor,
            ref_type="sale", ref_id=sale.id, note=sale.number,
        )
        db.add(
            SaleLine(
                sale_id=sale.id,
                variant_id=variant.id,
                bucket=line["bucket"],
                qty=line["qty"],
                unit_price=line["unit_price"],
                unit_cost=line["unit_cost"],
                line_total=line["line_total"],
            )
        )
    for pay in payments:
        _attach_payment(
            db, actor, amount=int(pay["amount"]), method=pay["method"],
            confirmed=bool(pay.get("confirmed", True)), kind="venta",
            sale=sale, layaway=None, note="",
        )
    _recompute_sale_balance(sale)
    finish(token, "sale", sale.id)
    audit(db, actor, "confirmar_venta", "sale", sale.id, sale.number)
    return sale


def next_sale_number(db: Session) -> str:
    from app.logic.common import next_number

    return next_number(db, "sale", "V")


def _recompute_sale_balance(sale: Sale) -> None:
    confirmed = sum(pay.amount for pay in sale.payments if pay.status == "confirmado" and pay.kind != "devolucion")
    refunded = sum(pay.amount for pay in sale.payments if pay.kind == "devolucion" and pay.status == "confirmado")
    sale.paid = confirmed - refunded
    sale.balance = sale.total - sale.paid
    if sale.balance < 0:
        sale.balance = 0


def _attach_payment(
    db: Session,
    actor: User,
    *,
    amount: int,
    method: str,
    confirmed: bool,
    kind: str,
    sale: Sale | None,
    layaway: Layaway | None,
    note: str,
    account_code: str | None = None,
    initial_load: bool = False,
) -> Payment:
    if amount <= 0:
        raise BusinessError("El pago debe ser mayor que cero.")
    if method not in METHOD_ACCOUNT and method != "breb":
        raise BusinessError("Elige un medio de pago válido.")
    account = account_by_code(db, account_code) if account_code else account_for_method(db, method)
    payment = Payment(
        sale_id=sale.id if sale else None,
        layaway_id=layaway.id if layaway else None,
        kind=kind,
        method=method,
        account_id=account.id,
        amount=amount,
        status="confirmado" if confirmed else "pendiente",
        user_id=actor.id,
        note=note,
    )
    db.add(payment)
    db.flush()
    if confirmed:
        if not initial_load:
            # El efectivo exige caja abierta; apply_account lo comprueba.
            apply_account(
                db, account, amount, kind, actor,
                note=note or (sale.number if sale else layaway.number if layaway else ""),
                ref_type="payment", ref_id=payment.id,
            )
        else:
            account.balance += amount
            from app.models import AccountMove

            db.add(
                AccountMove(
                    account_id=account.id,
                    amount=amount,
                    kind=kind,
                    ref_type="payment",
                    ref_id=payment.id,
                    user_id=actor.id,
                    note=note or "Carga inicial",
                )
            )
        open_session = find_open_session(db)
        payment.session_id = open_session.id if open_session else None
        if sale and kind in {"venta", "abono_fiado"}:
            sale.paid += amount
            sale.balance = max(sale.total - sale.paid, 0)
            if sale.balance == 0:
                sale.due_date = None
        if layaway and kind == "abono_separado":
            layaway.paid += amount
    return payment


def confirm_payment(db: Session, actor: User, payment: Payment) -> None:
    if payment.status != "pendiente":
        raise BusinessError("Ese pago ya estaba confirmado.")
    account = payment.account
    if account is None:
        raise BusinessError("El pago no tiene cuenta de destino.")
    apply_account(
        db, account, payment.amount, payment.kind, actor,
        note="Confirmación de pago pendiente", ref_type="payment", ref_id=payment.id,
    )
    payment.status = "confirmado"
    open_session = find_open_session(db)
    payment.session_id = open_session.id if open_session else None
    if payment.sale and payment.kind in {"venta", "abono_fiado"}:
        payment.sale.paid += payment.amount
        payment.sale.balance = max(payment.sale.total - payment.sale.paid, 0)
    if payment.layaway_id:
        layaway = db.get(Layaway, payment.layaway_id)
        if layaway and payment.kind == "abono_separado":
            layaway.paid += payment.amount
    audit(db, actor, "confirmar_pago", "payment", payment.id, str(payment.amount))


def add_credit_payment(
    db: Session,
    actor: User,
    sale: Sale,
    *,
    amount: int,
    method: str,
    confirmed: bool = True,
) -> Payment:
    require_open_session(db)
    if sale.status == "anulada":
        raise BusinessError("La venta está anulada.")
    if sale.balance <= 0:
        raise BusinessError("Esta venta no tiene saldo pendiente.")
    pending = sum(pay.amount for pay in sale.payments if pay.status == "pendiente")
    room = sale.balance - pending
    if amount > room:
        raise BusinessError("El abono supera el saldo todavía libre. Si hay un pago pendiente, confírmalo en vez de registrarlo otra vez.")
    if sale.customer is None:
        raise BusinessError("El fiado no tiene clienta.")
    payment = _attach_payment(
        db, actor, amount=amount, method=method, confirmed=confirmed,
        kind="abono_fiado", sale=sale, layaway=None, note=f"Abono {sale.number}",
    )
    audit(db, actor, "abono_fiado", "sale", sale.id, sale.number)
    return payment


def void_sale(db: Session, actor: User, sale: Sale, username: str, password: str, reason: str) -> None:
    authorizer = authorize(db, actor, username, password)
    if not (reason or "").strip():
        raise BusinessError("Escribe el motivo de la anulación.")
    if sale.status == "anulada":
        raise BusinessError("La venta ya está anulada.")
    if sale.source == "separado":
        raise BusinessError("Las entregas de separado se corrigen desde el separado, no anulando el comprobante.")
    if any(line.returned for line in sale.lines):
        raise BusinessError("Esta venta ya tiene devoluciones. Sigue por devolución, no por anulación.")
    for line in sale.lines:
        apply_stock(
            db, line.variant, line.bucket, line.qty, 0, "anulacion", line.unit_cost, authorizer,
            ref_type="sale", ref_id=sale.id, note=reason.strip(),
        )
        line.returned_stock = line.qty
    for payment in list(sale.payments):
        if payment.status == "pendiente":
            payment.status = "anulado"
            continue
        if payment.status != "confirmado" or payment.account is None:
            continue
        apply_account(
            db, payment.account, -payment.amount, "devolucion", authorizer,
            note=f"Anulación {sale.number}", ref_type="sale", ref_id=sale.id,
        )
        payment.status = "anulado"
    sale.status = "anulada"
    sale.paid = 0
    sale.balance = 0
    sale.note = (sale.note + "\n" if sale.note else "") + f"Anulada: {reason.strip()}"
    audit(db, authorizer, "anular_venta", "sale", sale.id, reason.strip())


def return_line(
    db: Session,
    actor: User,
    line: SaleLine,
    *,
    qty: int,
    disposition: str,
    reason: str,
    refund_amount: int,
    method: str,
    username: str,
    password: str,
) -> None:
    authorizer = authorize(db, actor, username, password)
    sale = line.sale
    if sale.status == "anulada":
        raise BusinessError("La venta está anulada.")
    if not (reason or "").strip():
        raise BusinessError("La devolución necesita un motivo.")
    if not line.variant.product.returnable:
        raise BusinessError(f"{line.variant.product.name} no admite devolución.")
    if qty <= 0 or qty > line.remaining:
        raise BusinessError("La cantidad a devolver no es válida.")
    if disposition not in {"stock", "no_vendible"}:
        raise BusinessError("Indica si vuelve a la venta o queda no vendible.")
    if refund_amount < 0:
        raise BusinessError("El valor a devolver no puede ser negativo.")
    max_refund = (line.line_total * qty) // line.qty
    if refund_amount > max_refund:
        raise BusinessError("El reembolso supera el valor de lo devuelto.")
    if disposition == "stock":
        apply_stock(
            db, line.variant, line.bucket, qty, 0, "devolucion", line.unit_cost, authorizer,
            ref_type="sale", ref_id=sale.id, note=reason.strip(),
        )
        line.returned_stock += qty
    else:
        apply_stock(
            db, line.variant, line.bucket, 0, 0, "devolucion_no_vendible", line.unit_cost, authorizer,
            ref_type="sale", ref_id=sale.id, note=reason.strip(),
        )
        # La pieza no vuelve al disponible: entra al depósito no vendible.
        if line.bucket == "unidad":
            line.variant.unsellable_units += qty
        elif line.bucket == "paquete":
            line.variant.unsellable_packages += qty
        else:
            line.variant.unsellable_grams += qty
        # El movimiento de cantidad cero no se guarda; dejamos constancia con un ajuste de nota en auditoría.
        from app.models import StockMove

        db.add(
            StockMove(
                variant_id=line.variant_id,
                bucket=line.bucket,
                qty=0,
                reserved_delta=0,
                reason="devolucion_no_vendible",
                unit_cost=line.unit_cost,
                user_id=authorizer.id,
                ref_type="sale",
                ref_id=sale.id,
                note=f"{qty} no vendible. {reason.strip()}",
            )
        )
        line.returned_unsellable += qty
    if refund_amount:
        account = account_for_method(db, method)
        apply_account(
            db, account, -refund_amount, "devolucion", authorizer,
            note=f"Devolución {sale.number}", ref_type="sale", ref_id=sale.id,
        )
        db.add(
            Payment(
                sale_id=sale.id,
                kind="devolucion",
                method=method,
                account_id=account.id,
                amount=refund_amount,
                status="confirmado",
                user_id=authorizer.id,
                note=reason.strip(),
            )
        )
        db.flush()
        _recompute_sale_balance(sale)
    if all(item.remaining == 0 for item in sale.lines):
        sale.status = "devuelta"
    else:
        sale.status = "devuelta_parcial"
    audit(db, authorizer, "devolucion", "sale", sale.id, reason.strip())


def create_layaway(
    db: Session,
    actor: User,
    *,
    customer_id: int,
    variant_id: int,
    bucket: str,
    qty: int,
    agreed_price: int,
    deposit: int,
    method: str,
    idempotency_key: str,
    due_days: int | None = None,
    admin_username: str = "",
    admin_password: str = "",
    initial_load: bool = False,
    account_code: str | None = None,
    note: str = "",
    allow_below_minimum: bool = False,
) -> Layaway:
    token = claim(db, idempotency_key, "layaway")
    if not initial_load:
        require_open_session(db)
    customer = db.get(Customer, customer_id)
    if customer is None or not customer.active:
        raise BusinessError("Elige una clienta.")
    require_consent(customer)
    variant = get_variant(db, variant_id)
    if bucket == "gramo" or (variant.unit == "unidad" and bucket != "unidad") or (
        variant.unit == "gramo" and bucket != "paquete"
    ):
        raise BusinessError("Se reserva la unidad o el paquete completo, no gramos sueltos.")
    if qty <= 0:
        raise BusinessError("La cantidad reservada debe ser mayor que cero.")
    if agreed_price <= 0:
        raise BusinessError("El precio acordado debe ser mayor que cero.")
    if deposit < 0 or deposit > agreed_price:
        raise BusinessError("El abono inicial no puede superar el precio acordado.")
    minimum_pct = setting_int(db, "layaway_min_pct")
    minimum = (agreed_price * minimum_pct + 99) // 100
    if deposit < minimum and not initial_load:
        if not (allow_below_minimum or actor.is_admin):
            authorize(db, actor, admin_username, admin_password)
        elif not actor.is_admin:
            authorize(db, actor, admin_username, admin_password)
    if available(variant, bucket) < qty:
        raise BusinessError("No hay existencia libre para reservar.")
    days = due_days if due_days is not None else setting_int(db, "layaway_days")
    if days <= 0:
        days = 30
    layaway = Layaway(
        number=next_layaway_number(db),
        customer_id=customer.id,
        variant_id=variant.id,
        bucket=bucket,
        qty=qty,
        agreed_price=agreed_price,
        paid=0,
        retention_percent=variant.product.retention_percent,
        status="activo",
        due_at=utcnow() + timedelta(days=days),
        user_id=actor.id,
        note=(note or "").strip(),
    )
    db.add(layaway)
    db.flush()
    apply_stock(
        db, variant, bucket, 0, qty, "reserva", bucket_cost(variant, bucket), actor,
        ref_type="layaway", ref_id=layaway.id, note=layaway.number,
    )
    if deposit:
        _attach_payment(
            db, actor, amount=deposit, method=method, confirmed=True, kind="abono_separado",
            sale=None, layaway=layaway, note=note or f"Abono inicial {layaway.number}",
            account_code=account_code if initial_load else None, initial_load=initial_load,
        )
    finish(token, "layaway", layaway.id)
    audit(db, actor, "crear_separado", "layaway", layaway.id, layaway.number)
    return layaway


def next_layaway_number(db: Session) -> str:
    from app.logic.common import next_number

    return next_number(db, "layaway", "SP")


def add_layaway_payment(
    db: Session,
    actor: User,
    layaway: Layaway,
    *,
    amount: int,
    method: str,
    confirmed: bool = True,
) -> Payment:
    require_open_session(db)
    if layaway.status != "activo":
        raise BusinessError("Este separado ya no recibe abonos.")
    pending = sum(
        pay.amount for pay in db.scalars(select(Payment).where(Payment.layaway_id == layaway.id, Payment.status == "pendiente")).all()
    )
    if amount > layaway.balance - pending:
        raise BusinessError("El abono supera el saldo del separado.")
    payment = _attach_payment(
        db, actor, amount=amount, method=method, confirmed=confirmed,
        kind="abono_separado", sale=None, layaway=layaway, note=f"Abono {layaway.number}",
    )
    audit(db, actor, "abono_separado", "layaway", layaway.id, layaway.number)
    return payment


def deliver_layaway(db: Session, actor: User, layaway: Layaway, idempotency_key: str) -> Sale:
    token = claim(db, idempotency_key, "deliver")
    session = require_open_session(db)
    if layaway.status != "activo":
        raise BusinessError("Este separado no está pendiente de entrega.")
    if layaway.paid < layaway.agreed_price:
        raise BusinessError("Todavía hay saldo. Completa el abono antes de entregar.")
    variant = layaway.variant
    unit_cost = bucket_cost(variant, layaway.bucket)
    sale = Sale(
        number=next_sale_number(db),
        customer_id=layaway.customer_id,
        user_id=actor.id,
        session_id=session.id,
        source="separado",
        layaway_id=layaway.id,
        subtotal=layaway.agreed_price,
        discount_pct=0,
        discount_amount=0,
        total=layaway.agreed_price,
        paid=layaway.agreed_price,
        balance=0,
        status="confirmada",
        note=f"Entrega del separado {layaway.number}. El dinero ya estaba recibido como anticipo.",
    )
    db.add(sale)
    db.flush()
    db.add(
        SaleLine(
            sale_id=sale.id,
            variant_id=variant.id,
            bucket=layaway.bucket,
            qty=layaway.qty,
            unit_price=layaway.agreed_price // layaway.qty,
            unit_cost=unit_cost,
            line_total=layaway.agreed_price,
        )
    )
    apply_stock(
        db, variant, layaway.bucket, -layaway.qty, -layaway.qty, "entrega_separado", unit_cost, actor,
        ref_type="sale", ref_id=sale.id, note=layaway.number,
    )
    layaway.status = "entregado"
    layaway.delivered_sale_id = sale.id
    layaway.closed_at = utcnow()
    finish(token, "sale", sale.id)
    audit(db, actor, "entregar_separado", "layaway", layaway.id, sale.number)
    return sale


def cancel_layaway(
    db: Session,
    actor: User,
    layaway: Layaway,
    *,
    reason: str,
    refund_account_code: str,
    username: str,
    password: str,
) -> None:
    authorizer = authorize(db, actor, username, password)
    if layaway.status != "activo":
        raise BusinessError("Este separado ya no se puede cancelar.")
    if not (reason or "").strip():
        raise BusinessError("Escribe el motivo de la cancelación.")
    retention = (layaway.paid * layaway.retention_percent + 50) // 100
    refund = layaway.paid - retention
    if refund > 0:
        account = account_by_code(db, refund_account_code)
        apply_account(
            db, account, -refund, "devolucion_separado", authorizer,
            note=f"Devolución {layaway.number}", ref_type="layaway", ref_id=layaway.id,
        )
        layaway.refund_account_id = account.id
    layaway.retention_amount = retention
    layaway.refund_amount = refund
    layaway.status = "cancelado"
    layaway.closed_at = utcnow()
    layaway.cancel_reason = reason.strip()
    apply_stock(
        db, layaway.variant, layaway.bucket, 0, -layaway.qty, "libera_reserva", 0, authorizer,
        ref_type="layaway", ref_id=layaway.id, note=reason.strip(),
    )
    audit(
        db, authorizer, "cancelar_separado", "layaway", layaway.id,
        f"retención {retention}, devuelto {refund}",
    )


def extend_layaway(
    db: Session,
    actor: User,
    layaway: Layaway,
    *,
    new_due: date,
    reason: str,
    username: str,
    password: str,
) -> None:
    authorizer = authorize(db, actor, username, password)
    if layaway.status != "activo":
        raise BusinessError("Solo se prorroga un separado activo.")
    if not (reason or "").strip():
        raise BusinessError("Escribe por qué se prorroga.")
    current_due = as_bogota(layaway.due_at)
    if current_due and new_due <= current_due.date():
        raise BusinessError("La nueva fecha tiene que ser posterior al vencimiento actual.")
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    layaway.due_at = datetime.combine(new_due, time(23, 59), tzinfo=ZoneInfo("America/Bogota"))
    layaway.note = (layaway.note + "\n" if layaway.note else "") + f"Prórroga: {reason.strip()}"
    audit(db, authorizer, "prorrogar_separado", "layaway", layaway.id, reason.strip())


def layaway_state(layaway: Layaway, alert_days: int) -> str:
    if layaway.status != "activo":
        return layaway.status
    due = as_bogota(layaway.due_at)
    if due is None:
        return "activo"
    today = today_bogota()
    if due.date() < today:
        return "vencido"
    if (due.date() - today).days <= alert_days:
        return "por_vencer"
    return "activo"


def days_open(layaway: Layaway) -> int:
    started = as_bogota(layaway.created_at)
    if started is None:
        return 0
    return max((today_bogota() - started.date()).days, 0)


def committed_advances(db: Session) -> int:
    total = db.scalar(select(func.coalesce(func.sum(Layaway.paid), 0)).where(Layaway.status == "activo"))
    return int(total or 0)


def search_customers(db: Session, query: str) -> list[Customer]:
    rows = db.scalars(select(Customer).where(Customer.active.is_(True)).order_by(Customer.name)).all()
    needle = fold(query.strip())
    if not needle:
        return list(rows)
    return [
        row for row in rows
        if needle in fold(row.name) or needle in fold(row.phone) or needle in fold(row.doc_id)
    ]


def search_variants(db: Session, query: str, limit: int = 30) -> list[Variant]:
    from app.models import Product

    rows = db.scalars(
        select(Variant).join(Product).where(Variant.active.is_(True), Product.active.is_(True))
    ).all()
    needle = fold(query.strip())
    if not needle:
        return []
    found = [
        row for row in rows
        if needle in fold(row.sku) or needle in fold(row.product.name) or needle in fold(row.label)
    ]
    return found[:limit]
