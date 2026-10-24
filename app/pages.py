"""Pantallas de KINKARA AFRO."""

from __future__ import annotations

import secrets
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backup import list_backups, make_backup, restore_into_session
from app.db import DATA
from app.importer import load_preview, parse_workbook, save_preview, template_xlsx
from app.logic.buying import (
    add_expense_category,
    cancel_purchase,
    create_expense,
    create_purchase,
    create_supplier,
    pay_expense,
    pay_purchase,
    receive_purchase,
)
from app.logic.caja import (
    caja_flows,
    close_caja,
    correct_caja,
    create_account,
    open_caja,
    owner_move,
    reconcile,
    transfer,
)
from app.logic.catalog import (
    add_variant,
    adjust_stock,
    apply_import_rows,
    create_product,
    mark_unsellable,
    open_package,
    set_price,
    update_product,
    variant_state,
)
from app.logic.common import (
    BusinessError,
    account_by_code,
    audit,
    count_active_admins,
    find_open_session,
    hash_password,
    set_setting,
    setting,
    setting_int,
    verify_password,
)
from app.logic.profit import dashboard, profit_report
from app.logic.sales import (
    add_credit_payment,
    add_layaway_payment,
    cancel_layaway,
    confirm_payment,
    confirm_sale,
    create_layaway,
    days_open,
    deliver_layaway,
    empty_cart,
    extend_layaway,
    layaway_state,
    put_line,
    put_payment,
    quote_cart,
    return_line,
    save_customer,
    search_customers,
    search_variants,
    void_sale,
    committed_advances,
)
from app.models import (
    Account,
    AccountMove,
    AuditLog,
    CashSession,
    Category,
    CostHistory,
    Customer,
    Expense,
    ExpenseCategory,
    ImportBatch,
    Layaway,
    Payment,
    PriceHistory,
    Product,
    Purchase,
    Sale,
    SaleLine,
    StockMove,
    Supplier,
    User,
    Variant,
)
from app.reports import TITLES, build_report, money_rows, to_pdf, to_xlsx
from app.utils import MESES, as_bogota, fold, fmt_dt, money, parse_date, parse_int, parse_money, period_bounds, today_bogota, utcnow
from app.web import (
    cart_of,
    get_db,
    redirect,
    render,
    require_admin,
    require_user,
    safe_next,
    save_cart,
)

router = APIRouter()


async def posted(request: Request):
    form = await request.form()
    token = form.get("_csrf") or request.headers.get("x-csrf-token")
    if not token or token != request.session.get("csrf"):
        raise BusinessError("El formulario venció. Vuelve a intentarlo.")
    return form


def pesos(form, name: str, label: str, **kwargs) -> int:
    try:
        return parse_money(form.get(name), label, **kwargs)
    except ValueError as exc:
        raise BusinessError(str(exc)) from exc


def entero(form, name: str, label: str, default: int | None = None, **kwargs) -> int:
    raw = form.get(name)
    if (raw is None or str(raw).strip() == "") and default is not None:
        return default
    try:
        return parse_int(raw, label, **kwargs)
    except ValueError as exc:
        raise BusinessError(str(exc)) from exc


def fecha(form, name: str, label: str) -> date:
    try:
        return parse_date(form.get(name), label)
    except ValueError as exc:
        raise BusinessError(str(exc)) from exc


def fecha_opcional(form, name: str) -> date | None:
    if not (form.get(name) or "").strip():
        return None
    return fecha(form, name, "La fecha")


def marcado(form, name: str) -> bool:
    return (form.get(name) or "") in {"1", "on", "si", "sí", "true"}


def claves(form) -> tuple[str, str]:
    return (form.get("admin_username") or ""), (form.get("admin_password") or "")


@router.get("/salud")
def salud():
    return {"ok": True}


@router.get("/sin-conexion")
def sin_conexion(request: Request, db: Session = Depends(get_db)):
    return render(request, db, "offline.html", partial=True)


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_db)):
    if request.session.get("uid"):
        return redirect("/")
    return render(request, db, "login.html", next=safe_next(request.query_params.get("next")))


@router.post("/login")
async def login_send(request: Request, db: Session = Depends(get_db), form=Depends(posted)):
    from app.logic.common import login_user

    user = login_user(db, form.get("username") or "", form.get("password") or "")
    request.session["uid"] = user.id
    return redirect(safe_next(form.get("next")))


@router.post("/logout")
async def logout(request: Request, form=Depends(posted)):
    request.session.clear()
    return redirect("/login")


@router.get("/")
def home(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    raw = (request.query_params.get("dia") or "").strip()
    chosen = None
    if raw:
        try:
            chosen = date.fromisoformat(raw)
        except ValueError:
            chosen = None
    data = dashboard(db, chosen)
    data.update(_home_view(db, data))
    return render(request, db, "dashboard.html", user=user, **data)


def _home_view(db: Session, data: dict) -> dict:
    from app.logic.profit import _returned_total

    variants = db.scalars(
        select(Variant).join(Product).where(Variant.active.is_(True), Product.active.is_(True))
    ).all()
    agotados = bajos = valor = 0
    for variant in variants:
        state = variant_state(variant)
        if state["agotado"]:
            agotados += 1
        elif state["bajo"]:
            bajos += 1
        if variant.unit == "gramo":
            grams = variant.stock_packages * (variant.package_weight_g or 0) + variant.stock_grams
            valor += grams * int(variant.cost)
        else:
            valor += int(variant.stock_units) * int(variant.cost)
    logs = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(5)).all()
    banner = (Path(__file__).resolve().parent / "static" / "img" / "banner.jpg").is_file()
    return {
        "agotados": agotados,
        "bajos": bajos,
        "referencias": len(variants),
        "inventario_valor": valor,
        "alert_count": agotados + bajos + int(data["due_count"]),
        "activity": [_activity_row(log) for log in logs],
        "chart": _sales_chart(data["sales_today"], _returned_total),
        "banner": banner,
        "today": today_bogota(),
    }


_ACTION_TITLE = {
    "confirmar_venta": "Venta registrada",
    "anular_venta": "Venta anulada",
    "devolucion": "Devolución",
    "confirmar_pago": "Pago confirmado",
    "abono_fiado": "Abono de fiado",
    "crear_cliente": "Nuevo cliente",
    "editar_cliente": "Cliente actualizado",
    "crear_separado": "Separado creado",
    "abono_separado": "Abono de separado",
    "entregar_separado": "Separado entregado",
    "cancelar_separado": "Separado cancelado",
    "prorrogar_separado": "Separado prorrogado",
    "ajuste_inventario": "Inventario ajustado",
    "no_vendible": "Pasó a no vendible",
    "abrir_paquete": "Paquete abierto",
    "crear_producto": "Producto creado",
    "editar_producto": "Producto editado",
    "crear_variante": "Variante creada",
    "editar_variante": "Variante editada",
    "cambiar_precio": "Precio actualizado",
    "corregir_costo": "Costo corregido",
    "abrir_caja": "Caja abierta",
    "cerrar_caja": "Caja cerrada",
    "corregir_cierre": "Cierre corregido",
    "registrar_gasto": "Gasto registrado",
    "crear_compra": "Compra creada",
    "recibir_compra": "Compra recibida",
    "pagar_compra": "Pago a proveedor",
    "anular_compra": "Compra anulada",
    "crear_proveedor": "Proveedor creado",
    "inicio_sesion": "Inicio de sesión",
    "traslado": "Traslado entre cuentas",
    "conciliar": "Conciliación",
    "crear_cuenta": "Cuenta creada",
    "aporte": "Aporte del dueño",
    "retiro": "Retiro del dueño",
    "prestamo_entra": "Préstamo recibido",
    "prestamo_sale": "Préstamo entregado",
    "autorizar_descuento": "Descuento autorizado",
    "crear_usuario": "Usuario creado",
    "editar_usuario": "Usuario editado",
    "cambiar_ajustes": "Ajustes actualizados",
    "cambiar_clave": "Clave actualizada",
    "crear_respaldo": "Respaldo creado",
    "descargar_respaldo": "Respaldo descargado",
    "restaurar_respaldo": "Respaldo restaurado",
    "descargar_reporte": "Reporte descargado",
}
_ACTION_ICON = {
    "confirmar_venta": "bag",
    "anular_venta": "bag",
    "devolucion": "bag",
    "crear_cliente": "userplus",
    "editar_cliente": "userplus",
    "ajuste_inventario": "clipboard",
    "no_vendible": "clipboard",
    "abrir_paquete": "package",
    "crear_producto": "package",
    "editar_producto": "package",
    "crear_variante": "package",
    "editar_variante": "package",
    "cambiar_precio": "coins",
    "corregir_costo": "coins",
    "abono_fiado": "hands",
    "confirmar_pago": "wallet",
    "abono_separado": "bookmark",
    "crear_separado": "bookmark",
    "entregar_separado": "bookmark",
    "cancelar_separado": "bookmark",
    "prorrogar_separado": "bookmark",
    "abrir_caja": "wallet",
    "cerrar_caja": "wallet",
    "corregir_cierre": "wallet",
    "registrar_gasto": "receipt",
    "pagar_compra": "receipt",
    "crear_compra": "truck",
    "recibir_compra": "truck",
    "anular_compra": "truck",
    "inicio_sesion": "users",
    "traslado": "bank",
    "conciliar": "bank",
    "crear_cuenta": "bank",
    "aporte": "piggy",
    "retiro": "piggy",
    "prestamo_entra": "piggy",
    "prestamo_sale": "piggy",
}
_ACTION_TONE = {
    "confirmar_venta": "rose",
    "crear_cliente": "lavender",
    "editar_cliente": "lavender",
    "ajuste_inventario": "peach",
    "no_vendible": "peach",
    "abono_fiado": "mint",
    "abono_separado": "peach",
    "crear_separado": "peach",
    "entregar_separado": "mint",
    "abrir_caja": "rose",
    "cerrar_caja": "lavender",
    "registrar_gasto": "peach",
    "inicio_sesion": "lavender",
}


def _when_label(value) -> str:
    current = as_bogota(value)
    if current is None:
        return "—"
    hour = current.hour % 12 or 12
    period = "a. m." if current.hour < 12 else "p. m."
    return f"{current.day} {MESES[current.month - 1]} {current.year} · {hour}:{current.minute:02d} {period}"


def _activity_row(log: AuditLog) -> dict:
    detail = (log.detail or "").strip()
    short = bool(detail) and len(detail) <= 22
    aside = detail if short else ""
    if log.action == "confirmar_pago" and detail.lstrip("-").isdigit():
        aside = money(int(detail))
        short = True
    return {
        "title": _ACTION_TITLE.get(log.action, log.action.replace("_", " ").capitalize()),
        "icon": _ACTION_ICON.get(log.action, "receipt"),
        "tone": _ACTION_TONE.get(log.action, "rose"),
        "when_label": _when_label(log.created_at),
        "subtitle": "" if short else detail,
        "aside": aside,
    }


def _sales_chart(sales, returned_total) -> dict:
    from app.utils import as_bogota

    buckets = [0] * 13
    for sale in sales:
        local = as_bogota(sale.created_at)
        if local is None:
            continue
        amount = int(sale.total) - int(returned_total(sale))
        hour = local.hour
        index = 0 if hour < 8 else 12 if hour > 20 else hour - 8
        buckets[index] += amount
    running = 0
    points = []
    for value in buckets:
        running += value
        points.append(running)
    width, height = 640, 210
    left, right, top, bottom = 12, 12, 16, 28
    inner_w = width - left - right
    inner_h = height - top - bottom
    peak = max(points) if points else 0
    coords = []
    for index, value in enumerate(points):
        x = left + inner_w * index / 12
        y = top + inner_h * (1 - (value / peak if peak else 0))
        coords.append((round(x, 1), round(y, 1)))
    line = " ".join(f"{x},{y}" for x, y in coords)
    area = f"M {coords[0][0]},{top + inner_h} " + " ".join(f"L {x},{y}" for x, y in coords) + f" L {coords[-1][0]},{top + inner_h} Z"
    names = ["8 a. m.", "10 a. m.", "12 p. m.", "2 p. m.", "4 p. m.", "6 p. m.", "8 p. m."]
    labels = [
        {"x": round(left + inner_w * (index * 2) / 12, 1), "text": text}
        for index, text in enumerate(names)
    ]
    return {
        "empty": peak <= 0,
        "line": line,
        "area": area,
        "dots": [{"x": x, "y": y} for x, y in coords],
        "labels": labels,
        "axis_y": top + inner_h,
    }


@router.get("/vender")
def vender(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    cart = cart_of(request)
    quick = db.scalars(
        select(Variant).join(Product).where(Product.quick_sale.is_(True), Variant.active.is_(True), Product.active.is_(True))
    ).all()
    return render(
        request, db, "pos.html", user=user, cart=cart, quote=quote_cart(db, cart), quick=list(quick),
        customers=db.scalars(select(Customer).where(Customer.active.is_(True)).order_by(Customer.name)).all(),
        max_pct=setting_int(db, "discount_max_pct"), min_total=setting_int(db, "discount_min_total"),
        due_default=(today_bogota() + timedelta(days=30)).isoformat(),
    )


@router.get("/vender/buscar")
def vender_buscar(request: Request, q: str = "", db: Session = Depends(get_db), user: User = Depends(require_user)):
    return render(request, db, "partials/search.html", partial=True, user=user, query=q, matches=search_variants(db, q))


@router.post("/vender/agregar")
async def vender_agregar(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    cart = cart_of(request)
    if (form.get("variant_id") or "").strip():
        variant = db.get(Variant, int(form.get("variant_id")))
        if variant is None:
            raise BusinessError("No encontré esa variante.")
        bucket = form.get("bucket") or ("unidad" if variant.unit == "unidad" else "paquete")
        put_line(cart, variant, bucket, entero(form, "qty", "La cantidad", default=1, minimum=1))
    else:
        query = (form.get("q") or "").strip()
        matches = search_variants(db, query, limit=15)
        exact = [item for item in matches if fold(item.sku) == fold(query)]
        chosen = exact[0] if len(exact) == 1 else (matches[0] if len(matches) == 1 else None)
        if chosen is None:
            raise BusinessError("Hay varias coincidencias o ninguna. Elige el producto en la lista.")
        put_line(cart, chosen, "unidad" if chosen.unit == "unidad" else "paquete", 1)
    save_cart(request, cart)
    return redirect("/vender")


@router.post("/vender/cantidad")
async def vender_cantidad(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    cart = cart_of(request)
    index = entero(form, "index", "La línea", minimum=0)
    qty = entero(form, "qty", "La cantidad", minimum=1)
    cart["lines"][index]["qty"] = qty
    save_cart(request, cart)
    return redirect("/vender")


@router.post("/vender/quitar")
async def vender_quitar(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    cart = cart_of(request)
    index = entero(form, "index", "La línea", minimum=0)
    cart["lines"].pop(index)
    save_cart(request, cart)
    return redirect("/vender")


@router.post("/vender/descuento")
async def vender_descuento(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    cart = cart_of(request)
    cart["discount_pct"] = entero(form, "discount_pct", "El descuento", default=0, minimum=0, maximum=100)
    save_cart(request, cart)
    return redirect("/vender")


@router.post("/vender/pago")
async def vender_pago(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    cart = cart_of(request)
    put_payment(cart, form.get("method") or "efectivo", pesos(form, "amount", "El pago", allow_zero=False), marcado(form, "confirmed"))
    save_cart(request, cart)
    return redirect("/vender")


@router.post("/vender/pago/quitar")
async def vender_pago_quitar(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    cart = cart_of(request)
    cart["payments"].pop(entero(form, "index", "El pago", minimum=0))
    save_cart(request, cart)
    return redirect("/vender")


@router.post("/vender/vaciar")
async def vender_vaciar(request: Request, user: User = Depends(require_user), form=Depends(posted)):
    save_cart(request, empty_cart())
    return redirect("/vender")


@router.post("/vender/confirmar")
async def vender_confirmar(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    cart = cart_of(request)
    if form.get("idem"):
        cart["idempotency_key"] = form.get("idem")
    cart["customer_id"] = int(form.get("customer_id")) if (form.get("customer_id") or "").strip() else None
    cart["due_date"] = form.get("due_date") or ""
    new_customer = None
    if (form.get("new_name") or "").strip():
        new_customer = {
            "name": form.get("new_name"), "phone": form.get("new_phone") or "", "doc_id": "",
            "email": "", "notes": "", "consent": marcado(form, "new_consent"),
        }
    username, password = claves(form)
    sale = confirm_sale(db, user, cart, admin_username=username, admin_password=password, new_customer=new_customer)
    save_cart(request, empty_cart())
    return redirect(f"/ventas/{sale.id}", request, f"Venta {sale.number} registrada.")


@router.get("/ventas")
def ventas(request: Request, desde: str = "", hasta: str = "", db: Session = Depends(get_db), user: User = Depends(require_user)):
    start = date.fromisoformat(desde) if desde else today_bogota().replace(day=1)
    end = date.fromisoformat(hasta) if hasta else today_bogota()
    opening, closing = period_bounds(start, end)
    sales = db.scalars(select(Sale).where(Sale.created_at >= opening, Sale.created_at <= closing).order_by(Sale.created_at.desc())).all()
    return render(request, db, "ventas.html", user=user, sales=sales, desde=start.isoformat(), hasta=end.isoformat())


@router.get("/ventas/{sale_id}")
def recibo(sale_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    sale = db.get(Sale, sale_id)
    if sale is None:
        raise BusinessError("No encontré ese comprobante.", redirect="/ventas")
    if sale.source == "separado" and sale.layaway_id:
        payments = db.scalars(select(Payment).where(Payment.layaway_id == sale.layaway_id).order_by(Payment.id)).all()
    else:
        payments = sale.payments
    return render(
        request, db, "receipt.html", user=user, sale=sale, payments=payments,
        city=setting(db, "business_city"), phone=setting(db, "business_phone"),
    )


@router.post("/ventas/{sale_id}/anular")
async def anular(sale_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    sale = db.get(Sale, sale_id)
    void_sale(db, user, sale, "", form.get("admin_password") or "", form.get("reason") or "")
    return redirect(f"/ventas/{sale.id}", request, "Venta anulada. El comprobante sigue visible.")


@router.post("/ventas/{sale_id}/devolver")
async def devolver(sale_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    line = db.get(SaleLine, int(form.get("line_id") or 0))
    if line is None or line.sale_id != sale_id:
        raise BusinessError("No encontré esa línea.")
    return_line(
        db, user, line, qty=entero(form, "qty", "La cantidad", minimum=1),
        disposition=form.get("disposition") or "stock", reason=form.get("reason") or "",
        refund_amount=pesos(form, "refund_amount", "El reembolso", allow_zero=True) if (form.get("refund_amount") or "").strip() else 0,
        method=form.get("method") or "efectivo", username="", password=form.get("admin_password") or "",
    )
    return redirect(f"/ventas/{sale_id}", request, "Devolución registrada.")


@router.post("/pagos/{payment_id}/confirmar")
async def confirmar_pago(payment_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise BusinessError("No encontré ese pago.")
    confirm_payment(db, user, payment)
    target = f"/ventas/{payment.sale_id}" if payment.sale_id else "/fiados"
    return redirect(target, request, "Pago confirmado. Ya entró a la cuenta.")


@router.get("/caja")
def caja(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    session = find_open_session(db)
    flows = caja_flows(db, session) if session else None
    history = db.scalars(select(CashSession).order_by(CashSession.opened_at.desc()).limit(30)).all()
    return render(
        request, db, "caja.html", user=user, session=session, flows=flows, history=history,
        accounts=db.scalars(select(Account).where(Account.active.is_(True)).order_by(Account.id)).all(),
        caja_balance=account_by_code(db, "caja").balance,
    )


@router.post("/caja/abrir")
async def caja_abrir(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    session = open_caja(db, user, pesos(form, "counted", "El efectivo contado"), form.get("note") or "")
    return redirect("/caja", request, f"Caja {session.number} abierta.")


@router.post("/caja/cerrar")
async def caja_cerrar(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    transfer_raw = (form.get("transfer_amount") or "0").strip()
    amount = pesos(form, "transfer_amount", "El traslado") if transfer_raw else 0
    account_id = int(form.get("transfer_account_id")) if (form.get("transfer_account_id") or "").strip() else None
    session = close_caja(
        db, user, counted=pesos(form, "counted", "El efectivo contado"),
        transfer_amount=amount, transfer_account_id=account_id, note=form.get("note") or "",
    )
    return redirect(f"/caja/{session.id}", request, "Caja cerrada. La base de mañana ya quedó.")


@router.get("/caja/{session_id}")
def cierre(session_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    session = _session_or_404(db, session_id)
    return render(request, db, "cierre.html", user=user, **_sheet(db, session, user))


@router.get("/caja/{session_id}/pdf")
def cierre_pdf(session_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    session = _session_or_404(db, session_id)
    sheet = _sheet(db, session, user)
    flows = sheet["flows"]
    rows = [
        ["Base anterior", session.opening_base], ["Contado al abrir", session.counted_open],
        ["Ventas en efectivo", flows["sales"]], ["Abonos de separados", flows["layaways"]],
        ["Abonos de fiados", flows["credits"]], ["Gastos", flows["expenses"]], ["Nómina", flows["payroll"]],
        ["Proveedores", flows["suppliers"]], ["Devoluciones", flows["refunds"]],
        ["Efectivo esperado", flows["expected"]], ["Efectivo contado", session.counted_close or 0],
        ["Diferencia", session.close_diff or 0], ["Traslado", session.transfer_amount],
        ["Base del día siguiente", session.closing_base or 0],
        ["Ventas entregadas (todos los medios)", sheet["delivered"]],
        ["Abonos de separados (todos los medios)", sheet["layaway_all"]],
    ]
    report = {"kind": "cierres", "title": f"Cierre {session.number}", "filters": fmt_dt(session.closed_at or session.opened_at), "generated": fmt_dt(utcnow()), "headers": ["Concepto", "Valor"], "rows": rows}
    return Response(to_pdf(report), media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{session.number}.pdf"'})


@router.post("/caja/{session_id}/corregir")
async def corregir_cierre(session_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    session = _session_or_404(db, session_id)
    correct_caja(
        db, user, session,
        counted=pesos(form, "counted", "El efectivo contado"),
        transfer_amount=pesos(form, "transfer_amount", "El traslado"),
        transfer_account_id=int(form.get("transfer_account_id")) if (form.get("transfer_account_id") or "").strip() else None,
        note=form.get("note") or "", username="", password=form.get("admin_password") or "",
    )
    return redirect(f"/caja/{session.id}", request, "Cierre corregido. Los movimientos anteriores quedaron marcados.")


def _session_or_404(db: Session, session_id: int) -> CashSession:
    session = db.get(CashSession, session_id)
    if session is None:
        raise BusinessError("No encontré ese turno.", redirect="/caja")
    return session


def _sheet(db: Session, session: CashSession, user: User) -> dict:
    flows = caja_flows(db, session)
    sales = db.scalars(select(Sale).where(Sale.session_id == session.id, Sale.status != "anulada")).all()
    delivered = 0
    for sale in sales:
        for line in sale.lines:
            if line.qty:
                delivered += (line.line_total * (line.qty - line.returned)) // line.qty
    payments = db.scalars(select(Payment).where(Payment.session_id == session.id, Payment.status == "confirmado")).all()
    layaway_all = sum(item.amount for item in payments if item.kind == "abono_separado")
    credit_all = sum(item.amount for item in payments if item.kind == "abono_fiado")
    latest = db.scalar(select(CashSession).order_by(CashSession.id.desc()))
    day_source = session.closed_at or session.opened_at
    day = as_bogota(day_source).date() if day_source else today_bogota()
    return {
        "session": session, "flows": flows, "delivered": delivered, "layaway_all": layaway_all,
        "credit_all": credit_all,
        "profit": profit_report(db, day, day) if user.is_admin else None,
        "accounts": db.scalars(select(Account).where(Account.active.is_(True))).all(),
        "can_correct": bool(latest and latest.id == session.id and find_open_session(db) is None and session.status == "cerrada"),
    }


@router.get("/cuentas")
def cuentas(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    accounts = db.scalars(select(Account).where(Account.active.is_(True)).order_by(Account.id)).all()
    return render(request, db, "cuentas.html", user=user, accounts=accounts, total=sum(item.balance for item in accounts), committed=committed_advances(db))


@router.post("/cuentas")
async def cuentas_crear(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    create_account(db, user, form.get("name") or "", form.get("kind") or "otro")
    return redirect("/cuentas", request, "Cuenta creada.")


@router.get("/cuentas/{account_id}")
def cuenta(account_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    account = db.get(Account, account_id)
    if account is None:
        raise BusinessError("No encontré esa cuenta.", redirect="/cuentas")
    moves = db.scalars(select(AccountMove).where(AccountMove.account_id == account.id).order_by(AccountMove.id.desc()).limit(200)).all()
    return render(request, db, "cuenta.html", user=user, account=account, moves=moves, committed=committed_advances(db))


@router.post("/cuentas/trasladar")
async def cuentas_trasladar(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    transfer(db, user, source_id=int(form.get("source_id")), target_id=int(form.get("target_id")), amount=pesos(form, "amount", "El valor", allow_zero=False), note=form.get("note") or "")
    return redirect("/cuentas", request, "Traslado hecho. El dinero total no cambió.")


@router.post("/cuentas/movimiento")
async def cuentas_movimiento(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    owner_move(db, user, account_id=int(form.get("account_id")), kind=form.get("kind") or "", amount=pesos(form, "amount", "El valor", allow_zero=False), note=form.get("note") or "")
    return redirect("/cuentas", request, "Movimiento registrado. No entra a las ventas ni a los gastos.")


@router.post("/cuentas/{account_id}/conciliar")
async def cuentas_conciliar(account_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    account = db.get(Account, account_id)
    reconcile(db, user, account, statement_balance=pesos(form, "statement_balance", "El saldo del extracto"), reason=form.get("reason") or "", username="", password=form.get("admin_password") or "")
    return redirect(f"/cuentas/{account_id}", request, "Conciliación registrada.")


@router.get("/productos")
def productos(request: Request, q: str = "", db: Session = Depends(get_db), user: User = Depends(require_user)):
    rows = db.scalars(select(Product).order_by(Product.name)).all()
    needle = fold(q)
    if needle:
        rows = [item for item in rows if needle in fold(item.name) or needle in fold(item.code)]
    return render(request, db, "productos.html", user=user, products=rows, q=q)


@router.get("/productos/nuevo")
def producto_nuevo(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return render(request, db, "producto_form.html", user=user, categories=db.scalars(select(Category).order_by(Category.name)).all())


@router.post("/productos")
async def producto_crear(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    product = create_product(
        db, user, code=form.get("code") or "", name=form.get("name") or "", category=form.get("category") or "",
        description=form.get("description") or "", returnable=marcado(form, "returnable"),
        retention_percent=entero(form, "retention_percent", "La retención", default=0, minimum=0, maximum=100),
        quick_sale=marcado(form, "quick_sale"), sku=form.get("sku") or "", length_in=form.get("length_in") or "",
        texture=form.get("texture") or "", color=form.get("color") or "", origin=form.get("origin") or "",
        hair_type=form.get("hair_type") or "", unit=form.get("unit") or "unidad",
        package_weight_g=entero(form, "package_weight_g", "El peso", default=0, minimum=0),
        package_price=pesos(form, "package_price", "El precio del paquete") if (form.get("package_price") or "").strip() else 0,
        price=pesos(form, "price", "El precio"),
        package_or_unit_cost=pesos(form, "cost", "El costo") if (form.get("cost") or "").strip() else 0,
        initial_qty=entero(form, "initial_qty", "La cantidad", default=0, minimum=0),
        initial_grams=entero(form, "initial_grams", "Los gramos", default=0, minimum=0),
        min_stock=entero(form, "min_stock", "El mínimo", default=0, minimum=0),
    )
    return redirect(f"/productos/{product.id}", request, "Producto creado.")


@router.get("/productos/{product_id}")
def producto(product_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    product = db.get(Product, product_id)
    if product is None:
        raise BusinessError("No encontré ese producto.", redirect="/productos")
    ids = [variant.id for variant in product.variants]
    prices = db.scalars(select(PriceHistory).where(PriceHistory.variant_id.in_(ids)).order_by(PriceHistory.id.desc()).limit(20)).all() if ids else []
    costs = db.scalars(select(CostHistory).where(CostHistory.variant_id.in_(ids)).order_by(CostHistory.id.desc()).limit(20)).all() if ids else []
    return render(request, db, "producto.html", user=user, product=product, prices=prices, costs=costs, categories=db.scalars(select(Category).order_by(Category.name)).all())


@router.post("/productos/{product_id}")
async def producto_editar(product_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    product = db.get(Product, product_id)
    update_product(
        db, user, product, name=form.get("name") or "", category=form.get("category") or "",
        description=form.get("description") or "", returnable=marcado(form, "returnable"),
        retention_percent=entero(form, "retention_percent", "La retención", default=0, minimum=0, maximum=100),
        quick_sale=marcado(form, "quick_sale"), active=marcado(form, "active"),
    )
    return redirect(f"/productos/{product.id}", request, "Ficha actualizada.")


@router.post("/productos/{product_id}/variantes")
async def variante_crear(product_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    product = db.get(Product, product_id)
    add_variant(
        db, user, product, sku=form.get("sku") or "", length_in=form.get("length_in") or "",
        texture=form.get("texture") or "", color=form.get("color") or "", origin=form.get("origin") or "",
        hair_type=form.get("hair_type") or "", unit=form.get("unit") or "unidad",
        package_weight_g=entero(form, "package_weight_g", "El peso", default=0, minimum=0),
        package_price=pesos(form, "package_price", "El precio del paquete") if (form.get("package_price") or "").strip() else 0,
        price=pesos(form, "price", "El precio"),
        package_or_unit_cost=pesos(form, "cost", "El costo") if (form.get("cost") or "").strip() else 0,
        initial_qty=entero(form, "initial_qty", "La cantidad", default=0, minimum=0),
        min_stock=entero(form, "min_stock", "El mínimo", default=0, minimum=0),
    )
    return redirect(f"/productos/{product.id}", request, "Variante agregada.")


@router.post("/variantes/{variant_id}/precio")
async def variante_precio(variant_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    variant = db.get(Variant, variant_id)
    package = pesos(form, "package_price", "El precio del paquete") if (form.get("package_price") or "").strip() else None
    set_price(db, user, variant, price=pesos(form, "price", "El precio"), package_price=package)
    return redirect(f"/productos/{variant.product_id}", request, "Precio actualizado. Quedó en el historial.")


@router.get("/inventario")
def inventario(request: Request, q: str = "", alertas: str = "", db: Session = Depends(get_db), user: User = Depends(require_user)):
    variants = db.scalars(select(Variant).join(Product).order_by(Product.name, Variant.sku)).all()
    needle = fold(q)
    rows = []
    for variant in variants:
        state = variant_state(variant)
        if needle and needle not in fold(variant.sku) and needle not in fold(variant.label):
            continue
        if alertas and not state["bajo"]:
            continue
        rows.append({"variant": variant, "state": state})
    return render(request, db, "inventario.html", user=user, rows=rows, q=q, alertas=bool(alertas))


@router.get("/inventario/movimientos")
def movimientos(request: Request, variant_id: int = 0, desde: str = "", hasta: str = "", db: Session = Depends(get_db), user: User = Depends(require_user)):
    start = date.fromisoformat(desde) if desde else today_bogota() - timedelta(days=30)
    end = date.fromisoformat(hasta) if hasta else today_bogota()
    opening, closing = period_bounds(start, end)
    query = select(StockMove).where(StockMove.created_at >= opening, StockMove.created_at <= closing).order_by(StockMove.id.desc())
    if variant_id:
        query = query.where(StockMove.variant_id == variant_id)
    return render(
        request, db, "movimientos.html", user=user,
        moves=db.scalars(query.limit(400)).all(),
        variants=db.scalars(select(Variant).order_by(Variant.sku)).all(),
        variant_id=variant_id, desde=start.isoformat(), hasta=end.isoformat(),
    )


@router.post("/inventario/abrir-paquete")
async def abrir_paquete(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    variant = db.get(Variant, int(form.get("variant_id") or 0))
    open_package(db, user, variant)
    return redirect(safe_next(form.get("volver") or "/inventario"), request, f"Abriste un paquete de {variant.package_weight_g} g.")


@router.post("/inventario/ajuste")
async def ajuste(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    variant = db.get(Variant, int(form.get("variant_id") or 0))
    username, password = claves(form)
    adjust_stock(db, variant, form.get("bucket") or "unidad", entero(form, "delta", "El ajuste"), form.get("reason") or "", user, username, password)
    return redirect("/inventario", request, "Ajuste registrado.")


@router.post("/inventario/no-vendible")
async def no_vendible(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    variant = db.get(Variant, int(form.get("variant_id") or 0))
    username, password = claves(form)
    mark_unsellable(db, variant, form.get("bucket") or "unidad", entero(form, "qty", "La cantidad", minimum=1), form.get("reason") or "", user, username, password)
    return redirect("/inventario", request, "La mercancía quedó como no vendible.")


@router.get("/inventario/carga")
def carga_form(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return render(request, db, "carga.html", user=user)


@router.get("/inventario/plantilla.xlsx")
def plantilla(user: User = Depends(require_admin)):
    return Response(template_xlsx(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": 'attachment; filename="inventario-inicial-kinkara.xlsx"'})


@router.post("/inventario/carga")
async def carga_revisar(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    upload = form.get("archivo")
    content = await upload.read()
    if len(content) > 2_000_000:
        raise BusinessError("El archivo pesa demasiado.")
    payload = parse_workbook(content, db)
    token = secrets.token_hex(16)
    save_preview(token, payload)
    request.session["import_token"] = token
    return redirect(f"/inventario/carga/vista?token={token}")


@router.get("/inventario/carga/vista")
def carga_vista(request: Request, token: str = "", db: Session = Depends(get_db), user: User = Depends(require_admin)):
    if token != request.session.get("import_token"):
        raise BusinessError("La vista previa no corresponde a tu sesión.", redirect="/inventario/carga")
    payload = load_preview(token)
    return render(request, db, "carga_preview.html", user=user, rows=payload["rows"], duplicate=payload["duplicate"], token=token, errors=sum(len(row["errors"]) for row in payload["rows"]))


@router.post("/inventario/carga/confirmar")
async def carga_confirmar(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    token = form.get("token") or ""
    if token != request.session.get("import_token"):
        raise BusinessError("La vista previa venció.", redirect="/inventario/carga")
    payload = load_preview(token)
    if any(row["errors"] for row in payload["rows"]):
        raise BusinessError("Corrige los errores de la plantilla antes de confirmar.")
    if payload["duplicate"] and (form.get("phrase") or "").strip() != "CARGAR DE NUEVO":
        raise BusinessError("Esta plantilla ya se cargó. Escribe CARGAR DE NUEVO si de verdad quieres sumarla otra vez.")
    stored = payload["hash"] if not payload["duplicate"] else f"{payload['hash']}:{secrets.token_hex(4)}"
    db.add(ImportBatch(file_hash=stored, rows=len(payload["rows"]), user_id=user.id, forced=bool(payload["duplicate"])))
    db.flush()
    count = apply_import_rows(db, user, [row["data"] for row in payload["rows"]])
    request.session["import_token"] = ""
    return redirect("/inventario", request, f"Carga confirmada: {count} filas. Quedaron los movimientos de apertura.")


@router.get("/clientes")
def clientes(request: Request, q: str = "", db: Session = Depends(get_db), user: User = Depends(require_user)):
    return render(request, db, "clientes.html", user=user, customers=search_customers(db, q), q=q)


@router.post("/clientes")
async def clientes_crear(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    customer = save_customer(db, user, name=form.get("name") or "", phone=form.get("phone") or "", doc_id=form.get("doc_id") or "", email=form.get("email") or "", notes=form.get("notes") or "", consent=marcado(form, "consent"))
    return redirect(f"/clientes/{customer.id}", request, "Clienta registrada.")


@router.get("/clientes/{customer_id}")
def cliente(customer_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise BusinessError("No encontré esa clienta.", redirect="/clientes")
    sales = db.scalars(select(Sale).where(Sale.customer_id == customer.id).order_by(Sale.created_at.desc())).all()
    layaways = db.scalars(select(Layaway).where(Layaway.customer_id == customer.id).order_by(Layaway.created_at.desc())).all()
    return render(request, db, "cliente.html", user=user, customer=customer, sales=sales, layaways=layaways)


@router.post("/clientes/{customer_id}")
async def cliente_editar(customer_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    save_customer(db, user, name=form.get("name") or "", phone=form.get("phone") or "", doc_id=form.get("doc_id") or "", email=form.get("email") or "", notes=form.get("notes") or "", consent=marcado(form, "consent"), customer_id=customer_id)
    return redirect(f"/clientes/{customer_id}", request, "Ficha actualizada.")


@router.get("/fiados")
def fiados(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    sales = db.scalars(select(Sale).where(Sale.balance > 0, Sale.status != "anulada").order_by(Sale.due_date)).all()
    return render(request, db, "fiados.html", user=user, sales=sales, total=sum(item.balance for item in sales), today=today_bogota())


@router.post("/fiados/{sale_id}/abono")
async def fiado_abono(sale_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    sale = db.get(Sale, sale_id)
    add_credit_payment(db, user, sale, amount=pesos(form, "amount", "El abono", allow_zero=False), method=form.get("method") or "efectivo", confirmed=marcado(form, "confirmed"))
    return redirect("/fiados", request, "Abono registrado.")


@router.get("/separados")
def separados(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    rows = db.scalars(select(Layaway).order_by(Layaway.created_at.desc())).all()
    alert = setting_int(db, "layaway_alert_days")
    items = [{"layaway": row, "state": layaway_state(row, alert), "days": days_open(row)} for row in rows]
    variants = db.scalars(select(Variant).join(Product).where(Variant.active.is_(True), Product.active.is_(True)).order_by(Product.name)).all()
    return render(
        request, db, "separados.html", user=user, items=items, committed=committed_advances(db),
        customers=db.scalars(select(Customer).where(Customer.active.is_(True), Customer.consent.is_(True)).order_by(Customer.name)).all(),
        variants=variants, days=setting_int(db, "layaway_days"), min_pct=setting_int(db, "layaway_min_pct"),
        idem=secrets.token_hex(16),
    )


@router.post("/separados")
async def separados_crear(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    variant = db.get(Variant, int(form.get("variant_id") or 0))
    bucket = "unidad" if variant and variant.unit == "unidad" else "paquete"
    username, password = claves(form)
    layaway = create_layaway(
        db, user, customer_id=int(form.get("customer_id") or 0), variant_id=variant.id, bucket=bucket,
        qty=entero(form, "qty", "La cantidad", default=1, minimum=1),
        agreed_price=pesos(form, "agreed_price", "El precio acordado", allow_zero=False),
        deposit=pesos(form, "deposit", "El abono"), method=form.get("method") or "efectivo",
        idempotency_key=form.get("idem") or secrets.token_hex(16),
        admin_username=username, admin_password=password, allow_below_minimum=marcado(form, "allow_below_minimum"),
    )
    return redirect(f"/separados/{layaway.id}", request, f"Separado {layaway.number} reservado.")


@router.get("/separados/{layaway_id}")
def separado(layaway_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    layaway = db.get(Layaway, layaway_id)
    if layaway is None:
        raise BusinessError("No encontré ese separado.", redirect="/separados")
    payments = db.scalars(select(Payment).where(Payment.layaway_id == layaway.id).order_by(Payment.id)).all()
    return render(
        request, db, "separado.html", user=user, layaway=layaway, payments=payments,
        state=layaway_state(layaway, setting_int(db, "layaway_alert_days")), days=days_open(layaway),
        accounts=db.scalars(select(Account).where(Account.active.is_(True))).all(), idem=secrets.token_hex(16),
    )


@router.post("/separados/{layaway_id}/abono")
async def separado_abono(layaway_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    layaway = db.get(Layaway, layaway_id)
    add_layaway_payment(db, user, layaway, amount=pesos(form, "amount", "El abono", allow_zero=False), method=form.get("method") or "efectivo", confirmed=marcado(form, "confirmed"))
    return redirect(f"/separados/{layaway_id}", request, "Abono registrado como anticipo.")


@router.post("/separados/{layaway_id}/entregar")
async def separado_entregar(layaway_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    layaway = db.get(Layaway, layaway_id)
    sale = deliver_layaway(db, user, layaway, form.get("idem") or secrets.token_hex(16))
    return redirect(f"/ventas/{sale.id}", request, "Entrega registrada. Ahora sí es una venta.")


@router.post("/separados/{layaway_id}/cancelar")
async def separado_cancelar(layaway_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    layaway = db.get(Layaway, layaway_id)
    cancel_layaway(db, user, layaway, reason=form.get("reason") or "", refund_account_code=form.get("refund_account") or "caja", username="", password=form.get("admin_password") or "")
    return redirect(f"/separados/{layaway_id}", request, "Separado cancelado. La retención quedó como otro ingreso.")


@router.post("/separados/{layaway_id}/prorrogar")
async def separado_prorrogar(layaway_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    layaway = db.get(Layaway, layaway_id)
    extend_layaway(db, user, layaway, new_due=fecha(form, "new_due", "La nueva fecha"), reason=form.get("reason") or "", username="", password=form.get("admin_password") or "")
    return redirect(f"/separados/{layaway_id}", request, "Plazo prorrogado.")


@router.get("/compras")
def compras(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    purchases = db.scalars(select(Purchase).order_by(Purchase.created_at.desc())).all()
    payable = sum(item.balance for item in purchases if item.status != "anulada")
    return render(
        request, db, "compras.html", user=user, purchases=purchases, payable=payable, today=today_bogota().isoformat(),
        due_default=(today_bogota() + timedelta(days=30)).isoformat(),
        suppliers=db.scalars(select(Supplier).where(Supplier.active.is_(True)).order_by(Supplier.name)).all(),
        variants=db.scalars(select(Variant).join(Product).where(Variant.active.is_(True)).order_by(Product.name)).all(),
    )


@router.post("/proveedores")
async def proveedor_crear(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    create_supplier(db, user, form.get("name") or "", form.get("phone") or "")
    return redirect("/compras", request, "Proveedor registrado.")


@router.post("/compras")
async def compra_crear(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    lines = []
    for variant_id, qty, cost in zip(form.getlist("variant_id"), form.getlist("qty"), form.getlist("unit_cost")):
        if not str(variant_id).strip() or not str(qty).strip():
            continue
        variant = db.get(Variant, int(variant_id))
        lines.append({
            "variant_id": variant.id,
            "bucket": "unidad" if variant.unit == "unidad" else "paquete",
            "qty": int(str(qty).strip()),
            "unit_cost": parse_money(str(cost), "El costo") if str(cost).strip() else 0,
        })
    purchase = create_purchase(
        db, user, supplier_id=int(form.get("supplier_id") or 0), lines=lines,
        shipping=pesos(form, "shipping", "El flete") if (form.get("shipping") or "").strip() else 0,
        ordered_at=fecha(form, "ordered_at", "La fecha"), due_date=fecha_opcional(form, "due_date"),
        note=form.get("note") or "", pay_amount=pesos(form, "pay_amount", "El pago") if (form.get("pay_amount") or "").strip() else 0,
        method=(form.get("method") or None) or None, receive_now=marcado(form, "receive_now"),
    )
    return redirect(f"/compras/{purchase.id}", request, f"Pedido {purchase.number} guardado.")


@router.get("/compras/{purchase_id}")
def compra(purchase_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    purchase = db.get(Purchase, purchase_id)
    if purchase is None:
        raise BusinessError("No encontré ese pedido.", redirect="/compras")
    return render(request, db, "compra.html", user=user, purchase=purchase)


@router.post("/compras/{purchase_id}/recibir")
async def compra_recibir(purchase_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    purchase = db.get(Purchase, purchase_id)
    receive_purchase(db, user, purchase)
    return redirect(f"/compras/{purchase.id}", request, "Mercancía recibida. El costo promedio ya incluye el flete.")


@router.post("/compras/{purchase_id}/pagar")
async def compra_pagar(purchase_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    purchase = db.get(Purchase, purchase_id)
    pay_purchase(db, user, purchase, pesos(form, "amount", "El pago", allow_zero=False), form.get("method") or "efectivo")
    return redirect(f"/compras/{purchase.id}", request, "Pago aplicado al pedido.")


@router.post("/compras/{purchase_id}/anular")
async def compra_anular(purchase_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    purchase = db.get(Purchase, purchase_id)
    cancel_purchase(db, user, purchase, form.get("reason") or "")
    return redirect(f"/compras/{purchase.id}", request, "Pedido anulado.")


@router.get("/gastos")
def gastos(request: Request, desde: str = "", hasta: str = "", categoria: str = "", db: Session = Depends(get_db), user: User = Depends(require_admin)):
    start = date.fromisoformat(desde) if desde else today_bogota().replace(day=1)
    end = date.fromisoformat(hasta) if hasta else today_bogota()
    expenses = db.scalars(select(Expense).where(Expense.spent_on >= start, Expense.spent_on <= end).order_by(Expense.spent_on.desc())).all()
    if categoria:
        expenses = [item for item in expenses if item.category.slug == categoria]
    return render(
        request, db, "gastos.html", user=user, expenses=expenses, desde=start.isoformat(), hasta=end.isoformat(),
        categoria=categoria, today=today_bogota().isoformat(),
        categories=db.scalars(select(ExpenseCategory).where(ExpenseCategory.active.is_(True)).order_by(ExpenseCategory.name)).all(),
    )


@router.post("/gastos")
async def gasto_crear(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    attachment = ""
    upload = form.get("soporte")
    if upload is not None and getattr(upload, "filename", ""):
        suffix = Path(upload.filename).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".pdf"}:
            raise BusinessError("El soporte debe ser imagen o PDF.")
        content = await upload.read()
        if len(content) > 5_000_000:
            raise BusinessError("El soporte pesa más de 5 MB.")
        folder = DATA / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        attachment = f"{secrets.token_hex(8)}{suffix}"
        (folder / attachment).write_bytes(content)
    create_expense(
        db, user, category_slug=form.get("category") or "otros",
        amount=pesos(form, "amount", "El valor", allow_zero=False),
        spent_on=fecha(form, "spent_on", "La fecha"), description=form.get("description") or "",
        paid=marcado(form, "paid"), method=(form.get("method") or None) or None, attachment=attachment,
    )
    return redirect("/gastos", request, "Gasto registrado.")


@router.post("/gastos/categorias")
async def gasto_categoria(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    add_expense_category(db, user, form.get("name") or "")
    return redirect("/gastos", request, "Categoría creada.")


@router.post("/gastos/{expense_id}/pagar")
async def gasto_pagar(expense_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    expense = db.get(Expense, expense_id)
    pay_expense(db, user, expense, form.get("method") or "efectivo")
    return redirect("/gastos", request, "Gasto pagado.")


@router.get("/gastos/{expense_id}/soporte")
def gasto_soporte(expense_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    expense = db.get(Expense, expense_id)
    if expense is None or not expense.attachment:
        raise BusinessError("Este gasto no tiene soporte.", redirect="/gastos")
    path = (DATA / "uploads" / expense.attachment).resolve()
    if path.parent != (DATA / "uploads").resolve() or not path.exists():
        raise BusinessError("No encontré el archivo.", redirect="/gastos")
    return FileResponse(path)


@router.get("/ganancias")
def ganancias(request: Request, desde: str = "", hasta: str = "", db: Session = Depends(get_db), user: User = Depends(require_admin)):
    start = date.fromisoformat(desde) if desde else today_bogota().replace(day=1)
    end = date.fromisoformat(hasta) if hasta else today_bogota()
    return render(request, db, "ganancias.html", user=user, report=profit_report(db, start, end))


@router.get("/reportes")
def reportes(request: Request, tipo: str = "ventas", desde: str = "", hasta: str = "", categoria: int = 0, producto: int = 0, clienta: int = 0, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    start = date.fromisoformat(desde) if desde else today_bogota().replace(day=1)
    end = date.fromisoformat(hasta) if hasta else today_bogota()
    report = build_report(db, tipo, start=start, end=end, category_id=categoria or None, product_id=producto or None, customer_id=clienta or None)
    query = request.url.query
    return render(
        request, db, "reportes.html", user=user, report=report, shown=money_rows(report)["rows"], tipo=tipo,
        desde=start.isoformat(), hasta=end.isoformat(), query=query, kinds=list(TITLES.items()),
        categories=db.scalars(select(Category).order_by(Category.name)).all(),
        products=db.scalars(select(Product).order_by(Product.name)).all(),
        customers=db.scalars(select(Customer).order_by(Customer.name)).all(),
    )


@router.get("/reportes/{kind}/excel")
def reporte_excel(kind: str, request: Request, desde: str = "", hasta: str = "", categoria: int = 0, producto: int = 0, clienta: int = 0, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    report = _filtered_report(db, kind, desde, hasta, categoria, producto, clienta)
    audit(db, user, "descargar_reporte", "report", None, kind)
    return Response(to_xlsx(report), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="{kind}.xlsx"'})


@router.get("/reportes/{kind}/pdf")
def reporte_pdf(kind: str, request: Request, desde: str = "", hasta: str = "", categoria: int = 0, producto: int = 0, clienta: int = 0, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    report = _filtered_report(db, kind, desde, hasta, categoria, producto, clienta)
    audit(db, user, "descargar_reporte", "report", None, kind)
    return Response(to_pdf(report), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{kind}.pdf"'})


def _filtered_report(db, kind, desde, hasta, categoria, producto, clienta):
    start = date.fromisoformat(desde) if desde else today_bogota().replace(day=1)
    end = date.fromisoformat(hasta) if hasta else today_bogota()
    return build_report(db, kind, start=start, end=end, category_id=categoria or None, product_id=producto or None, customer_id=clienta or None)


@router.get("/usuarios")
def usuarios(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return render(request, db, "usuarios.html", user=user, people=db.scalars(select(User).order_by(User.username)).all())


@router.post("/usuarios")
async def usuario_crear(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    username = (form.get("username") or "").strip().lower()
    password = form.get("password") or ""
    if len(password) < 8:
        raise BusinessError("La contraseña necesita al menos 8 caracteres.")
    if db.scalar(select(User).where(User.username == username)):
        raise BusinessError("Ese usuario ya existe.")
    person = User(username=username, full_name=(form.get("full_name") or "").strip() or username, password_hash=hash_password(password), role=form.get("role") or "cajero", active=True)
    db.add(person)
    db.flush()
    audit(db, user, "crear_usuario", "user", person.id, person.username)
    return redirect("/usuarios", request, "Usuario creado.")


@router.post("/usuarios/{user_id}")
async def usuario_editar(user_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    person = db.get(User, user_id)
    if person is None:
        raise BusinessError("No encontré ese usuario.")
    new_role = form.get("role") or person.role
    new_active = marcado(form, "active")
    if person.role == "admin" and person.active and (new_role != "admin" or not new_active) and count_active_admins(db) <= 1:
        raise BusinessError("Tiene que quedar al menos una administración activa.")
    person.full_name = (form.get("full_name") or person.full_name).strip()
    person.role = new_role
    person.active = new_active
    if (form.get("password") or "").strip():
        if len(form.get("password")) < 8:
            raise BusinessError("La contraseña necesita al menos 8 caracteres.")
        person.password_hash = hash_password(form.get("password"))
    audit(db, user, "editar_usuario", "user", person.id, person.username)
    return redirect("/usuarios", request, "Usuario actualizado.")


@router.get("/ajustes")
def ajustes(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    keys = ["business_name", "business_city", "business_phone", "business_nit", "discount_max_pct", "discount_min_total", "layaway_days", "layaway_alert_days", "layaway_min_pct", "breb_account", "show_demo_hint"]
    values = {key: setting(db, key) for key in keys}
    return render(request, db, "ajustes.html", user=user, values=values, accounts=db.scalars(select(Account).where(Account.active.is_(True))).all())


@router.post("/ajustes")
async def ajustes_guardar(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    for key in ("business_name", "business_city", "business_phone", "business_nit", "discount_max_pct", "discount_min_total", "layaway_days", "layaway_alert_days", "layaway_min_pct", "breb_account"):
        set_setting(db, key, (form.get(key) or "").strip())
    set_setting(db, "show_demo_hint", "1" if marcado(form, "show_demo_hint") else "0")
    audit(db, user, "cambiar_ajustes", "setting", None, "ajustes del negocio")
    return redirect("/ajustes", request, "Ajustes guardados.")


@router.post("/cuenta/clave")
async def cambiar_clave(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user), form=Depends(posted)):
    if not verify_password(form.get("current") or "", user.password_hash):
        raise BusinessError("La clave actual no coincide.")
    if len(form.get("new") or "") < 8:
        raise BusinessError("La clave nueva necesita al menos 8 caracteres.")
    user.password_hash = hash_password(form.get("new"))
    audit(db, user, "cambiar_clave", "user", user.id, user.username)
    return redirect("/ajustes" if user.is_admin else "/", request, "Contraseña actualizada.")


@router.get("/auditoria")
def auditoria(request: Request, desde: str = "", hasta: str = "", db: Session = Depends(get_db), user: User = Depends(require_admin)):
    start = date.fromisoformat(desde) if desde else today_bogota() - timedelta(days=7)
    end = date.fromisoformat(hasta) if hasta else today_bogota()
    opening, closing = period_bounds(start, end)
    logs = db.scalars(select(AuditLog).where(AuditLog.created_at >= opening, AuditLog.created_at <= closing).order_by(AuditLog.id.desc()).limit(300)).all()
    return render(request, db, "auditoria.html", user=user, logs=logs, desde=start.isoformat(), hasta=end.isoformat())


@router.get("/respaldo")
def respaldo(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    backups = list_backups()
    latest = backups[0].name if backups else ""
    return render(request, db, "respaldo.html", user=user, backups=backups, latest=latest)


@router.post("/respaldo")
async def respaldo_crear(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    path = make_backup(request.app.state.engine)
    if path is None:
        raise BusinessError("Este respaldo automático es para la base SQLite. En PostgreSQL usa scripts/backup.sh.")
    audit(db, user, "crear_respaldo", "backup", None, path.name)
    return redirect("/respaldo", request, f"Copia {path.name} lista. Descárgala para guardarla fuera de este equipo.")


@router.get("/respaldo/{name}")
def respaldo_bajar(name: str, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    path = _backup_path(name)
    audit(db, user, "descargar_respaldo", "backup", None, name)
    return FileResponse(path, filename=name)


@router.post("/respaldo/restaurar")
async def respaldo_restaurar(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), form=Depends(posted)):
    path = _backup_path(form.get("archivo") or "")
    restore_into_session(db, path, form.get("phrase") or "")
    audit(db, user, "restaurar_respaldo", "backup", None, path.name)
    return redirect("/respaldo", request, "Restauración hecha. Revisa que los saldos coincidan con la copia.")


def _backup_path(name: str) -> Path:
    import re

    if not re.fullmatch(r"kinkara-\d{8}-\d{6}\.db", name or ""):
        raise BusinessError("Esa copia no existe.")
    path = (DATA / "backups" / name).resolve()
    if path.parent != (DATA / "backups").resolve() or not path.exists():
        raise BusinessError("Esa copia no existe.")
    return path


# Silencia el import de money si el linter lo mira; se usa en plantillas vía filtro.
_ = money
