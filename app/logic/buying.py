"""Compras, fletes, cuentas por pagar y gastos."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logic.common import (
    BusinessError,
    account_for_method,
    apply_account,
    apply_stock,
    audit,
    bucket_cost,
    expense_category,
    get_variant,
    next_number,
    remember_cost,
    require_open_session,
)
from app.models import (
    Expense,
    ExpenseCategory,
    Purchase,
    PurchaseLine,
    Supplier,
    SupplierPayment,
    User,
    Variant,
)
from app.utils import div_round, today_bogota, utcnow


def weighted_cost(old_qty: int, old_cost: int, new_qty: int, new_cost: int) -> int:
    total = old_qty + new_qty
    if total <= 0:
        return new_cost
    return div_round(old_qty * old_cost + new_qty * new_cost, total)


def split_freight(merch_values: list[int], shipping: int) -> list[int]:
    if not merch_values:
        return []
    if shipping <= 0:
        return [0 for _ in merch_values]
    base = sum(merch_values)
    if base <= 0:
        shares = []
        allocated = 0
        for index in range(len(merch_values)):
            if index == len(merch_values) - 1:
                shares.append(shipping - allocated)
            else:
                share = shipping // len(merch_values)
                allocated += share
                shares.append(share)
        return shares
    shares = []
    allocated = 0
    for index, merch in enumerate(merch_values):
        if index == len(merch_values) - 1:
            shares.append(shipping - allocated)
        else:
            share = (shipping * merch) // base
            allocated += share
            shares.append(share)
    return shares


def create_supplier(db: Session, user: User, name: str, phone: str = "", email: str = "", notes: str = "") -> Supplier:
    clean = " ".join((name or "").split())
    if len(clean) < 2:
        raise BusinessError("El proveedor necesita un nombre.")
    supplier = Supplier(name=clean[:160], phone=(phone or "").strip(), email=(email or "").strip(), notes=(notes or "").strip())
    db.add(supplier)
    db.flush()
    audit(db, user, "crear_proveedor", "supplier", supplier.id, supplier.name)
    return supplier


def create_purchase(
    db: Session,
    user: User,
    *,
    supplier_id: int,
    lines: list[dict],
    shipping: int,
    ordered_at: date,
    due_date: date | None,
    note: str,
    pay_amount: int,
    method: str | None,
    receive_now: bool,
) -> Purchase:
    if not user.is_admin:
        raise BusinessError("Solo administración registra compras.")
    supplier = db.get(Supplier, supplier_id)
    if supplier is None or not supplier.active:
        raise BusinessError("Elige un proveedor.")
    if shipping < 0:
        raise BusinessError("El flete no puede ser negativo.")
    if not lines:
        raise BusinessError("La compra no tiene productos.")
    built = []
    for raw in lines:
        variant = get_variant(db, int(raw["variant_id"]))
        bucket = raw["bucket"]
        qty = int(raw["qty"])
        unit_cost = int(raw["unit_cost"])
        if qty <= 0 or unit_cost < 0:
            raise BusinessError(f"Revisa cantidad y costo de {variant.label}.")
        if variant.unit == "unidad" and bucket != "unidad":
            raise BusinessError(f"{variant.label} se compra por unidad.")
        if variant.unit == "gramo" and bucket != "paquete":
            raise BusinessError(f"{variant.label} se compra por paquetes. El peso fijo reparte el costo por gramo.")
        built.append((variant, bucket, qty, unit_cost, qty * unit_cost))
    merch_values = [item[4] for item in built]
    freight = split_freight(merch_values, shipping)
    merchandise = sum(merch_values)
    total = merchandise + shipping
    if pay_amount < 0 or pay_amount > total:
        raise BusinessError("El pago no puede superar el total del pedido.")
    if pay_amount < total and due_date is None:
        raise BusinessError("Una compra con saldo necesita fecha de vencimiento.")
    purchase = Purchase(
        number=next_number(db, "purchase", "OC"),
        supplier_id=supplier.id,
        status="pedida",
        merchandise=merchandise,
        shipping=shipping,
        total=total,
        paid=0,
        balance=total,
        ordered_at=ordered_at,
        due_date=due_date,
        user_id=user.id,
        note=(note or "").strip(),
    )
    db.add(purchase)
    db.flush()
    for (variant, bucket, qty, unit_cost, _merch), share in zip(built, freight):
        final_unit = unit_cost + div_round(share, qty)
        purchase.lines.append(
            PurchaseLine(
                variant_id=variant.id,
                bucket=bucket,
                qty=qty,
                unit_cost=unit_cost,
                freight_share=share,
                final_unit_cost=final_unit,
            )
        )
    db.flush()
    if pay_amount:
        if not method:
            raise BusinessError("Elige el medio del pago al proveedor.")
        _pay(db, user, purchase, pay_amount, method, "Pago al crear el pedido")
    if receive_now:
        receive_purchase(db, user, purchase)
    audit(db, user, "crear_compra", "purchase", purchase.id, purchase.number)
    return purchase


def receive_purchase(db: Session, user: User, purchase: Purchase) -> Purchase:
    if not user.is_admin:
        raise BusinessError("Solo administración recibe mercancía.")
    if purchase.status == "recibida":
        raise BusinessError("Este pedido ya fue recibido. No se volvió a sumar al inventario.")
    if purchase.status == "anulada":
        raise BusinessError("El pedido está anulado.")
    for line in purchase.lines:
        variant = line.variant
        _receive_line(db, user, variant, line)
    purchase.status = "recibida"
    purchase.received_at = utcnow()
    audit(db, user, "recibir_compra", "purchase", purchase.id, purchase.number)
    return purchase


def _receive_line(db: Session, user: User, variant: Variant, line: PurchaseLine) -> None:
    if line.bucket == "unidad":
        old_qty = variant.stock_units
        variant.cost = weighted_cost(old_qty, variant.cost, line.qty, line.final_unit_cost)
        remember_cost(db, variant, user, f"compra {line.purchase.number}")
        apply_stock(
            db, variant, "unidad", line.qty, 0, "compra", line.final_unit_cost, user,
            ref_type="purchase", ref_id=line.purchase_id, note=line.purchase.number,
        )
        return
    weight = variant.package_weight_g
    if weight <= 0:
        raise BusinessError(f"{variant.label} no tiene peso de paquete: no puedo repartir el flete.")
    per_gram = div_round(line.final_unit_cost, weight)
    old_equiv = variant.stock_packages * weight + variant.stock_grams
    variant.cost = weighted_cost(old_equiv, variant.cost, line.qty * weight, per_gram)
    remember_cost(db, variant, user, f"compra {line.purchase.number}")
    apply_stock(
        db, variant, "paquete", line.qty, 0, "compra", line.final_unit_cost, user,
        ref_type="purchase", ref_id=line.purchase_id, note=line.purchase.number,
    )


def pay_purchase(db: Session, user: User, purchase: Purchase, amount: int, method: str, note: str = "") -> SupplierPayment:
    if not user.is_admin:
        raise BusinessError("Solo administración paga proveedores.")
    if purchase.status == "anulada":
        raise BusinessError("El pedido está anulado.")
    return _pay(db, user, purchase, amount, method, note or f"Abono {purchase.number}")


def _pay(db: Session, user: User, purchase: Purchase, amount: int, method: str, note: str) -> SupplierPayment:
    if amount <= 0:
        raise BusinessError("El pago debe ser mayor que cero.")
    if amount > purchase.balance:
        raise BusinessError("El pago supera el saldo del pedido.")
    account = account_for_method(db, method)
    if account.kind == "caja":
        require_open_session(db)
    move = apply_account(
        db, account, -amount, "abono_proveedor", user,
        note=note, ref_type="purchase", ref_id=purchase.id,
    )
    payment = SupplierPayment(
        purchase_id=purchase.id,
        account_id=account.id,
        amount=amount,
        method=method,
        user_id=user.id,
        session_id=move.session_id if move else None,
        note=note,
    )
    db.add(payment)
    purchase.paid += amount
    purchase.balance -= amount
    if purchase.balance == 0:
        purchase.due_date = None
    db.flush()
    audit(db, user, "pagar_compra", "purchase", purchase.id, purchase.number)
    return payment


def cancel_purchase(db: Session, user: User, purchase: Purchase, reason: str) -> None:
    if not user.is_admin:
        raise BusinessError("Solo administración anula pedidos.")
    if purchase.status == "recibida":
        raise BusinessError("La mercancía ya entró. Corrígela con un ajuste de inventario, no borrando la compra.")
    if purchase.status == "anulada":
        raise BusinessError("El pedido ya está anulado.")
    if not (reason or "").strip():
        raise BusinessError("Escribe el motivo.")
    for payment in list(purchase.payments):
        apply_account(
            db, payment.account, payment.amount, "devolucion", user,
            note=f"Anulación {purchase.number}", ref_type="purchase", ref_id=purchase.id,
        )
    purchase.paid = 0
    purchase.balance = 0
    purchase.status = "anulada"
    purchase.note = (purchase.note + "\n" if purchase.note else "") + reason.strip()
    audit(db, user, "anular_compra", "purchase", purchase.id, reason.strip())


def create_expense(
    db: Session,
    user: User,
    *,
    category_slug: str,
    amount: int,
    spent_on: date,
    description: str,
    paid: bool,
    method: str | None,
    attachment: str = "",
) -> Expense:
    if not user.is_admin:
        raise BusinessError("Solo administración registra gastos.")
    if amount <= 0:
        raise BusinessError("El gasto debe ser mayor que cero.")
    if not (description or "").strip():
        raise BusinessError("Describe el gasto.")
    category = expense_category(db, category_slug)
    expense = Expense(
        category_id=category.id,
        amount=amount,
        spent_on=spent_on or today_bogota(),
        description=description.strip(),
        user_id=user.id,
        status="pendiente",
        attachment=attachment,
    )
    db.add(expense)
    db.flush()
    if paid:
        if not method:
            raise BusinessError("Elige la cuenta con la que se pagó.")
        _pay_expense(db, user, expense, method)
    audit(db, user, "registrar_gasto", "expense", expense.id, category.name)
    return expense


def pay_expense(db: Session, user: User, expense: Expense, method: str) -> None:
    if not user.is_admin:
        raise BusinessError("Solo administración paga gastos.")
    if expense.status == "pagado":
        raise BusinessError("Este gasto ya está pagado.")
    _pay_expense(db, user, expense, method)


def _pay_expense(db: Session, user: User, expense: Expense, method: str) -> None:
    account = account_for_method(db, method)
    kind = "nomina" if expense.category.slug == "nomina" else "gasto"
    if account.kind == "caja":
        require_open_session(db)
    move = apply_account(
        db, account, -expense.amount, kind, user,
        note=expense.description, ref_type="expense", ref_id=expense.id,
    )
    expense.status = "pagado"
    expense.account_id = account.id
    expense.paid_at = utcnow()
    expense.session_id = move.session_id if move else None


def add_expense_category(db: Session, user: User, name: str) -> ExpenseCategory:
    if not user.is_admin:
        raise BusinessError("Solo administración crea categorías.")
    clean = (name or "").strip()
    if len(clean) < 3:
        raise BusinessError("La categoría necesita un nombre.")
    from app.utils import slugify

    slug = slugify(clean)
    if db.scalar(select(ExpenseCategory).where(ExpenseCategory.slug == slug)):
        raise BusinessError("Esa categoría ya existe.")
    category = ExpenseCategory(slug=slug, name=clean[:80], active=True)
    db.add(category)
    db.flush()
    return category


# Reexport del promedio para las pruebas y para no duplicar la fórmula.
def cost_pool_after_receipt(variant: Variant) -> int:
    if variant.unit == "gramo":
        return variant.stock_packages * variant.package_weight_g + variant.stock_grams
    return variant.stock_units


def line_enters_at(line: PurchaseLine) -> int:
    return bucket_cost(line.variant, line.bucket)
