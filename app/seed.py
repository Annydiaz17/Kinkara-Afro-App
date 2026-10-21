"""Datos iniciales. La caja arranca en $66.500, como la mañana del ejemplo del 13 de octubre."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logic.catalog import add_variant, create_product
from app.logic.common import DEFAULTS, hash_password
from app.logic.sales import create_layaway, save_customer
from app.models import Account, AccountMove, Category, ExpenseCategory, Payment, Setting, User
from app.utils import utcnow

ADMIN_PASSWORD = "Afro.2026"
CASHIER_PASSWORD = "Caja.2026"

ACCOUNTS = (
    ("caja", "Caja", "caja"),
    ("ahorro", "Ahorro", "ahorro"),
    ("nequi", "Nequi", "billetera"),
    ("daviplata", "Daviplata", "billetera"),
    ("bancolombia", "Bancolombia", "banco"),
    ("datafono", "Datáfono", "datafono"),
)

CATEGORIES = (
    "Cabello natural",
    "Cabello sintético",
    "Extensiones",
    "Pelucas",
    "Accesorios",
)

EXPENSES = (
    ("arriendo", "Arriendo"),
    ("servicios", "Servicios"),
    ("transporte", "Transporte"),
    ("publicidad", "Publicidad"),
    ("insumos", "Insumos"),
    ("nomina", "Nómina"),
    ("otros", "Otros"),
)


def ensure_settings(db: Session) -> None:
    for key, value in DEFAULTS.items():
        if db.get(Setting, key) is None:
            db.add(Setting(key=key, value=value))


def seed_core(db: Session) -> User:
    ensure_settings(db)
    for name in CATEGORIES:
        if db.scalar(select(Category).where(Category.name == name)) is None:
            db.add(Category(name=name))
    for slug, name in EXPENSES:
        if db.scalar(select(ExpenseCategory).where(ExpenseCategory.slug == slug)) is None:
            db.add(ExpenseCategory(slug=slug, name=name))
    for code, name, kind in ACCOUNTS:
        if db.scalar(select(Account).where(Account.code == code)) is None:
            db.add(Account(code=code, name=name, kind=kind, balance=0, active=True, system=True))
    db.flush()
    admin = db.scalar(select(User).where(User.username == "admin"))
    if admin is None:
        admin = User(
            username="admin",
            full_name="Administración",
            password_hash=hash_password(ADMIN_PASSWORD),
            role="admin",
            active=True,
        )
        db.add(admin)
    if db.scalar(select(User).where(User.username == "caja")) is None:
        db.add(
            User(
                username="caja",
                full_name="Caja",
                password_hash=hash_password(CASHIER_PASSWORD),
                role="cajero",
                active=True,
            )
        )
    db.flush()
    return admin


def seed_demo(db: Session) -> None:
    admin = seed_core(db)
    peluca = create_product(
        db, admin,
        code="PELUCA",
        name="Peluca lace front",
        category="Pelucas",
        description="Peluca con encaje frontal. No admite devolución una vez abierta.",
        returnable=False,
        retention_percent=20,
        sku="LF-18-BW-1B",
        length_in="18",
        texture="body wave",
        color="1B",
        origin="brasileño",
        hair_type="humano",
        unit="unidad",
        price=280_000,
        package_or_unit_cost=155_000,
        initial_qty=2,
        min_stock=1,
    )
    add_variant(
        db, admin, peluca, sku="LF-20-DW-4", length_in="20", texture="deep wave", color="4",
        origin="brasileño", hair_type="humano", unit="unidad", price=340_000,
        package_or_unit_cost=190_000, initial_qty=1, min_stock=1,
    )
    add_variant(
        db, admin, peluca, sku="LF-16-KC-99J", length_in="16", texture="kinky curly", color="99J",
        origin="peruano", hair_type="humano", unit="unidad", price=260_000,
        package_or_unit_cost=140_000, initial_qty=2, min_stock=1,
    )
    reserved = add_variant(
        db, admin, peluca, sku="LF-22-LW-1B", length_in="22", texture="loose wave", color="1B",
        origin="brasileño", hair_type="humano", unit="unidad", price=390_000,
        package_or_unit_cost=210_000, initial_qty=1, min_stock=1,
    )
    natural = create_product(
        db, admin,
        code="BR-NAT",
        name="Cabello natural brasileño",
        category="Cabello natural",
        description="Paquete de 100 g. Se puede vender cerrado o abrir para vender por gramos.",
        returnable=False,
        retention_percent=15,
        sku="BR-18-BW-1B",
        length_in="18",
        texture="body wave",
        color="1B",
        origin="brasileño",
        hair_type="humano",
        unit="gramo",
        package_weight_g=100,
        package_price=180_000,
        price=1_800,
        package_or_unit_cost=95_000,
        initial_qty=6,
        min_stock=2,
    )
    add_variant(
        db, admin, natural, sku="BR-20-DW-1B", length_in="20", texture="deep wave", color="1B",
        origin="brasileño", hair_type="humano", unit="gramo", package_weight_g=100,
        package_price=210_000, price=2_100, package_or_unit_cost=110_000, initial_qty=4, min_stock=2,
    )
    add_variant(
        db, admin, natural, sku="BR-16-KC-NAT", length_in="16", texture="kinky curly", color="natural",
        origin="brasileño", hair_type="humano", unit="gramo", package_weight_g=100,
        package_price=160_000, price=1_600, package_or_unit_cost=85_000, initial_qty=3, min_stock=1,
    )
    kane = create_product(
        db, admin, code="KANE", name="Kanekalon", category="Cabello sintético",
        description="Madeja para trenzas y peinados.", returnable=True, retention_percent=0,
        sku="KN-1B", color="1B", hair_type="sintético", unit="unidad", price=12_000,
        package_or_unit_cost=5_000, initial_qty=15, min_stock=4,
    )
    add_variant(db, admin, kane, sku="KN-99J", color="99J", hair_type="sintético", unit="unidad", price=12_000, package_or_unit_cost=5_000, initial_qty=10, min_stock=4)
    add_variant(db, admin, kane, sku="KN-27613", color="27/613", hair_type="sintético", unit="unidad", price=14_000, package_or_unit_cost=6_000, initial_qty=8, min_stock=3)
    create_product(
        db, admin, code="CLIP", name="Extensión clip-in", category="Extensiones",
        description="Clip-in de 18 pulgadas.", returnable=True, retention_percent=10,
        sku="CL-18-1B", length_in="18", texture="body wave", color="1B", hair_type="humano",
        unit="unidad", price=95_000, package_or_unit_cost=48_000, initial_qty=3, min_stock=1,
    )
    accessories = (
        ("PEINETA", "Peineta afro", "PN-AFRO", 8_000, 2_500, 20, 5),
        ("CEPILLO", "Cepillo de cerdas", "CP-CERDA", 18_000, 7_000, 2, 6),
        ("GEL", "Gel fijador", "GEL-FIX", 12_000, 4_500, 0, 4),
        ("ESPUMA", "Espuma moldeadora", "ESP-MOL", 15_000, 6_000, 9, 4),
        ("KIT", "Kit de cuidado", "KIT-CUID", 45_000, 18_000, 4, 2),
    )
    for code, name, sku, price, cost, qty, minimum in accessories:
        create_product(
            db, admin, code=code, name=name, category="Accesorios",
            description="Accesorio de precio fijo.", returnable=True, retention_percent=0,
            quick_sale=True, sku=sku, unit="unidad", price=price, package_or_unit_cost=cost,
            initial_qty=qty, min_stock=minimum,
        )
    maria = save_customer(
        db, admin, name="María José Rentería", phone="3005550148", doc_id="1010101010",
        email="", notes="Le gusta el loose wave.", consent=True,
    )
    save_customer(
        db, admin, name="Luisa Fernanda Gómez", phone="3105550192", doc_id="",
        email="", notes="", consent=True,
    )
    caja = db.scalar(select(Account).where(Account.code == "caja"))
    caja.balance = 66_500
    layaway = create_layaway(
        db, admin,
        customer_id=maria.id,
        variant_id=reserved.id,
        bucket="unidad",
        qty=1,
        agreed_price=390_000,
        deposit=100_000,
        method="efectivo",
        idempotency_key="seed-layaway-maria",
        initial_load=True,
        account_code="ahorro",
        note="Anticipo anterior a la apertura del sistema. El dinero está en ahorro.",
    )
    past = utcnow() - timedelta(days=25)
    layaway.created_at = past
    layaway.due_at = utcnow() + timedelta(days=5)
    for payment in db.scalars(select(Payment).where(Payment.layaway_id == layaway.id)).all():
        payment.created_at = past
    for move in db.scalars(select(AccountMove).where(AccountMove.ref_type == "payment")).all():
        move.created_at = past
    db.flush()
