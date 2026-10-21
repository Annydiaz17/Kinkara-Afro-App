"""Reglas de dinero: la hoja del 13 de octubre no puede volver a contar anticipos como utilidad."""

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import build_engine, session_factory
from app.logic.buying import create_purchase, split_freight, weighted_cost
from app.logic.caja import caja_flows, close_caja, open_caja, owner_move, transfer
from app.logic.catalog import create_product, open_package
from app.logic.common import AlreadyDone, BusinessError, find_open_session, login_user
from app.logic.profit import profit_report
from app.logic.sales import (
    AlreadyDone,
    cancel_layaway,
    confirm_sale,
    create_layaway,
    deliver_layaway,
    empty_cart,
    put_line,
    put_payment,
    return_line,
    save_customer,
)
from app.models import Account, Base, Sale, User, Variant
from app.seed import ADMIN_PASSWORD, CASHIER_PASSWORD, seed_core
from app.utils import today_bogota


@pytest.fixture
def db() -> Session:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = session_factory(engine)()
    seed_core(session)
    session.commit()
    yield session
    session.close()
    engine.dispose()


def admin(db: Session) -> User:
    return db.scalar(select(User).where(User.username == "admin"))


def cashier(db: Session) -> User:
    return db.scalar(select(User).where(User.username == "caja"))


def account(db: Session, code: str) -> Account:
    return db.scalar(select(Account).where(Account.code == code))


def money_total(db: Session) -> int:
    return sum(row.balance for row in db.scalars(select(Account)).all())


def sellable(db: Session, user: User, *, price: int, cost: int, qty: int, code: str = "P1", sku: str = "SKU1") -> Variant:
    product = create_product(
        db, user, code=code, name="Pieza de prueba", category="Pelucas", sku=sku,
        unit="unidad", price=price, package_or_unit_cost=cost, initial_qty=qty, returnable=True,
    )
    return product.variants[0]


def test_promedio_y_reparto_de_flete():
    assert weighted_cost(2, 50_000, 1, 110_000) == 70_000
    assert weighted_cost(0, 0, 1, 110_000) == 110_000
    assert split_freight([100_000], 10_000) == [10_000]
    assert split_freight([100_000, 50_000], 15_000) == [10_000, 5_000]
    assert sum(split_freight([80_000, 40_000, 30_000], 17_000)) == 17_000


def test_flete_entra_al_costo(db: Session):
    user = admin(db)
    variant = sellable(db, user, price=180_000, cost=0, qty=0, code="WIG", sku="WIG-1")
    create_purchase(
        db, user, supplier_id=_supplier(db, user),
        lines=[{"variant_id": variant.id, "bucket": "unidad", "qty": 1, "unit_cost": 100_000}],
        shipping=10_000, ordered_at=today_bogota(), due_date=today_bogota() + timedelta(days=15),
        note="Peluca de muestra", pay_amount=0, method=None, receive_now=True,
    )
    assert variant.cost == 110_000
    assert variant.stock_units == 1


def test_hoja_13_octubre(db: Session):
    user = admin(db)
    caja = account(db, "caja")
    ahorro = account(db, "ahorro")
    caja.balance = 66_500
    variant = sellable(db, user, price=42_000, cost=20_000, qty=5)
    open_caja(db, user, 66_500, "Base de la mañana")
    customer = save_customer(db, user, name="Clienta del día", phone="3000000001", consent=True)
    cart = empty_cart()
    put_line(cart, variant, "unidad", 1)
    put_payment(cart, "efectivo", 42_000)
    sale = confirm_sale(db, user, cart)
    assert sale.total == 42_000
    assert sale.number.startswith("V-")
    create_layaway(
        db, user, customer_id=customer.id, variant_id=variant.id, bucket="unidad", qty=1,
        agreed_price=200_000, deposit=150_000, method="efectivo",
        idempotency_key="sep-13",
    )
    before = money_total(db)
    flows = caja_flows(db, find_open_session(db))
    assert flows["sales"] == 42_000
    assert flows["layaways"] == 150_000
    assert flows["expected"] == 258_500
    closed = close_caja(db, user, counted=258_500, transfer_amount=200_000, transfer_account_id=ahorro.id, note="Como la hoja")
    assert closed.close_diff == 0
    assert closed.closing_base == 58_500
    assert caja.balance == 58_500
    assert ahorro.balance == 200_000
    assert money_total(db) == before
    report = profit_report(db, today_bogota(), today_bogota())
    assert report["net_sales"] == 42_000
    assert report["cogs"] == 20_000
    assert report["gross"] == 22_000
    assert report["layaway_advances_excluded"] == 150_000
    assert report["net"] == 22_000


def test_no_se_vende_de_mas_ni_se_duplica(db: Session):
    user = admin(db)
    variant = sellable(db, user, price=10_000, cost=4_000, qty=1)
    open_caja(db, user, 0)
    cart = empty_cart()
    put_line(cart, variant, "unidad", 2)
    put_payment(cart, "efectivo", 20_000)
    with pytest.raises(BusinessError, match="suficiente"):
        confirm_sale(db, user, cart)
    db.rollback()
    # La semilla quedó confirmada; reabrimos el escenario en la misma sesión limpia.
    user = admin(db)
    variant = sellable(db, user, price=10_000, cost=4_000, qty=1, code="P2", sku="SKU2")
    open_caja(db, user, 0)
    cart = empty_cart()
    put_line(cart, variant, "unidad", 1)
    put_payment(cart, "efectivo", 10_000)
    sale = confirm_sale(db, user, cart)
    again = empty_cart()
    again["idempotency_key"] = cart["idempotency_key"]
    put_line(again, variant, "unidad", 1)
    put_payment(again, "efectivo", 10_000)
    with pytest.raises(AlreadyDone):
        confirm_sale(db, user, again)
    assert variant.stock_units == 0
    assert db.scalar(select(Sale).where(Sale.id == sale.id))


def test_apertura_de_paquete_y_venta_por_gramos(db: Session):
    user = admin(db)
    product = create_product(
        db, user, code="CAB", name="Brasileño", category="Cabello natural", sku="B18",
        unit="gramo", package_weight_g=100, package_price=180_000, price=1_800,
        package_or_unit_cost=90_000, initial_qty=3, min_stock=1,
    )
    variant = product.variants[0]
    open_package(db, user, variant)
    assert variant.stock_packages == 2
    assert variant.stock_grams == 100
    open_caja(db, user, 0)
    cart = empty_cart()
    put_line(cart, variant, "gramo", 40)
    put_payment(cart, "efectivo", 72_000)
    confirm_sale(db, user, cart)
    assert variant.stock_grams == 60
    too_much = empty_cart()
    put_line(too_much, variant, "gramo", 70)
    put_payment(too_much, "efectivo", 126_000)
    with pytest.raises(BusinessError, match="gramos"):
        confirm_sale(db, user, too_much)


def test_descuento_del_cajero(db: Session):
    boss = admin(db)
    caja_user = cashier(db)
    variant = sellable(db, boss, price=100_000, cost=40_000, qty=4)
    open_caja(db, caja_user, 0)
    cart = empty_cart()
    put_line(cart, variant, "unidad", 1)
    cart["discount_pct"] = 10
    put_payment(cart, "efectivo", 90_000)
    with pytest.raises(BusinessError, match="autorización"):
        confirm_sale(db, caja_user, cart)
    db.rollback()
    boss = admin(db)
    caja_user = cashier(db)
    variant = sellable(db, boss, price=200_000, cost=80_000, qty=3, code="BIG", sku="BIG")
    open_caja(db, caja_user, 0)
    allowed = empty_cart()
    put_line(allowed, variant, "unidad", 1)
    allowed["discount_pct"] = 10
    put_payment(allowed, "efectivo", 180_000)
    sale = confirm_sale(db, caja_user, allowed)
    assert sale.total == 180_000
    blocked = empty_cart()
    put_line(blocked, variant, "unidad", 1)
    blocked["discount_pct"] = 15
    put_payment(blocked, "efectivo", 170_000)
    with pytest.raises(BusinessError, match="autorización"):
        confirm_sale(db, caja_user, blocked, admin_username="admin", admin_password="no")
    granted = empty_cart()
    put_line(granted, variant, "unidad", 1)
    granted["discount_pct"] = 15
    put_payment(granted, "efectivo", 170_000)
    sale = confirm_sale(db, caja_user, granted, admin_username="admin", admin_password=ADMIN_PASSWORD)
    assert sale.total == 170_000
    assert sale.discount_auth_id == boss.id


def test_devolucion_solo_si_el_producto_lo_permite(db: Session):
    user = admin(db)
    variant = sellable(db, user, price=50_000, cost=20_000, qty=2)
    open_caja(db, user, 20_000)
    caja = account(db, "caja")
    cart = empty_cart()
    put_line(cart, variant, "unidad", 1)
    put_payment(cart, "efectivo", 50_000)
    sale = confirm_sale(db, user, cart)
    variant.product.returnable = False
    with pytest.raises(BusinessError, match="no admite"):
        return_line(
            db, user, sale.lines[0], qty=1, disposition="stock", reason="no le sirvió",
            refund_amount=50_000, method="efectivo", username="admin", password=ADMIN_PASSWORD,
        )
    variant.product.returnable = True
    return_line(
        db, user, sale.lines[0], qty=1, disposition="stock", reason="cambio de opinión",
        refund_amount=50_000, method="efectivo", username="admin", password=ADMIN_PASSWORD,
    )
    assert variant.stock_units == 2
    assert caja.balance == 20_000
    report = profit_report(db, today_bogota(), today_bogota())
    assert report["net_sales"] == 0
    assert report["cogs"] == 0


def test_separado_se_reconoce_al_entregar_y_la_retencion_no_es_venta(db: Session):
    user = admin(db)
    variant = sellable(db, user, price=100_000, cost=40_000, qty=1, code="SEP", sku="SEP")
    variant.product.retention_percent = 20
    customer = save_customer(db, user, name="Ana Ruiz", phone="3010000002", consent=True)
    open_caja(db, user, 0)
    layaway = create_layaway(
        db, user, customer_id=customer.id, variant_id=variant.id, bucket="unidad", qty=1,
        agreed_price=100_000, deposit=100_000, method="efectivo", idempotency_key="entrega",
    )
    assert variant.reserved_units == 1
    assert variant.stock_units == 1
    early = profit_report(db, today_bogota(), today_bogota())
    assert early["net_sales"] == 0
    assert early["layaway_advances_excluded"] == 100_000
    sale = deliver_layaway(db, user, layaway, "entrega-1")
    assert sale.source == "separado"
    assert variant.stock_units == 0
    assert variant.reserved_units == 0
    with pytest.raises(BusinessError):
        deliver_layaway(db, user, layaway, "entrega-2")
    delivered = profit_report(db, today_bogota(), today_bogota())
    assert delivered["net_sales"] == 100_000
    assert delivered["cogs"] == 40_000
    assert delivered["layaway_advances_excluded"] == 0

    other = sellable(db, user, price=80_000, cost=30_000, qty=1, code="CAN", sku="CAN")
    other.product.retention_percent = 20
    cancel = create_layaway(
        db, user, customer_id=customer.id, variant_id=other.id, bucket="unidad", qty=1,
        agreed_price=80_000, deposit=50_000, method="nequi", idempotency_key="cancela",
    )
    cancel_layaway(
        db, user, cancel, reason="La clienta desistió", refund_account_code="nequi",
        username="admin", password=ADMIN_PASSWORD,
    )
    assert other.reserved_units == 0
    assert other.stock_units == 1
    assert account(db, "nequi").balance == 10_000
    final = profit_report(db, today_bogota(), today_bogota())
    assert final["retentions"] == 10_000
    assert final["net_sales"] == 100_000


def test_traslado_y_retiro_no_son_ganancia(db: Session):
    user = admin(db)
    account(db, "caja").balance = 80_000
    open_caja(db, user, 80_000)
    before = money_total(db)
    transfer(db, user, source_id=account(db, "caja").id, target_id=account(db, "ahorro").id, amount=30_000, note="Guardar")
    assert money_total(db) == before
    assert account(db, "ahorro").balance == 30_000
    owner_move(db, user, account_id=account(db, "caja").id, kind="retiro", amount=10_000, note="Retiro personal")
    report = profit_report(db, today_bogota(), today_bogota())
    assert report["net_sales"] == 0
    assert report["expenses"] == 0
    assert account(db, "caja").balance == 40_000


def test_login_no_revela_si_el_usuario_existe(db: Session):
    with pytest.raises(BusinessError, match="incorrectos"):
        login_user(db, "nadie", "Afro.2026")
    with pytest.raises(BusinessError, match="incorrectos"):
        login_user(db, "admin", "mala")
    cashier(db).active = False
    db.flush()
    with pytest.raises(BusinessError, match="desactivada"):
        login_user(db, "caja", CASHIER_PASSWORD)
    assert login_user(db, "admin", ADMIN_PASSWORD).role == "admin"


def _supplier(db: Session, user: User) -> int:
    from app.logic.buying import create_supplier

    return create_supplier(db, user, "Proveedor demo").id
