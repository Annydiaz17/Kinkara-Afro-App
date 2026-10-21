"""Productos, variantes, inventario y carga inicial."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.logic.common import (
    apply_stock,
    audit,
    authorize,
    available,
    bucket_cost,
    category_by_name,
    get_variant,
    remember_cost,
    remember_price,
)
from app.logic.common import BusinessError
from app.models import Product, StockMove, User, Variant
from app.utils import div_round, norm_code


def _clean_text(value: str | None, limit: int = 40) -> str:
    return (value or "").strip()[:limit]


def create_product(
    db: Session,
    user: User,
    *,
    code: str,
    name: str,
    category: str,
    description: str = "",
    returnable: bool = False,
    retention_percent: int = 20,
    quick_sale: bool = False,
    sku: str,
    length_in: str = "",
    texture: str = "",
    color: str = "",
    origin: str = "",
    hair_type: str = "",
    unit: str,
    package_weight_g: int = 0,
    package_price: int = 0,
    price: int = 0,
    package_or_unit_cost: int = 0,
    initial_qty: int = 0,
    initial_grams: int = 0,
    min_stock: int = 0,
) -> Product:
    clean_code = norm_code(code, "El código del producto")
    if db.scalar(select(Product).where(Product.code == clean_code)):
        raise BusinessError(f"Ya existe un producto con el código {clean_code}.")
    clean_name = (name or "").strip()
    if not clean_name:
        raise BusinessError("El producto necesita un nombre.")
    if retention_percent < 0 or retention_percent > 100:
        raise BusinessError("La retención del separado va de 0 a 100%.")
    product = Product(
        code=clean_code,
        name=clean_name[:160],
        category_id=category_by_name(db, category).id,
        description=(description or "").strip(),
        returnable=returnable,
        retention_percent=retention_percent,
        quick_sale=quick_sale and unit == "unidad",
    )
    db.add(product)
    db.flush()
    add_variant(
        db,
        user,
        product,
        sku=sku,
        length_in=length_in,
        texture=texture,
        color=color,
        origin=origin,
        hair_type=hair_type,
        unit=unit,
        package_weight_g=package_weight_g,
        package_price=package_price,
        price=price,
        package_or_unit_cost=package_or_unit_cost,
        initial_qty=initial_qty,
        initial_grams=initial_grams,
        min_stock=min_stock,
    )
    audit(db, user, "crear_producto", "product", product.id, product.code)
    return product


def add_variant(
    db: Session,
    user: User,
    product: Product,
    *,
    sku: str,
    length_in: str = "",
    texture: str = "",
    color: str = "",
    origin: str = "",
    hair_type: str = "",
    unit: str,
    package_weight_g: int = 0,
    package_price: int = 0,
    price: int = 0,
    package_or_unit_cost: int = 0,
    initial_qty: int = 0,
    initial_grams: int = 0,
    min_stock: int = 0,
) -> Variant:
    clean_sku = norm_code(sku, "El SKU")
    if db.scalar(select(Variant).where(Variant.sku == clean_sku)):
        raise BusinessError(f"Ya existe una variante con el SKU {clean_sku}.")
    if unit not in {"unidad", "gramo"}:
        raise BusinessError("La unidad de inventario es unidad o gramo.")
    if price < 0 or package_or_unit_cost < 0 or package_price < 0:
        raise BusinessError("Los precios y el costo no pueden ser negativos.")
    if initial_qty < 0 or initial_grams < 0 or min_stock < 0:
        raise BusinessError("Las cantidades no pueden ser negativas.")
    if unit == "gramo":
        if package_weight_g <= 0:
            raise BusinessError("El paquete de cabello necesita un peso fijo en gramos.")
        if package_price <= 0 and price <= 0:
            raise BusinessError("Define el precio del paquete o el precio por gramo.")
        cost = div_round(package_or_unit_cost, package_weight_g) if package_weight_g else 0
    else:
        package_weight_g = 0
        package_price = 0
        initial_grams = 0
        cost = package_or_unit_cost
    variant = Variant(
        product=product,
        sku=clean_sku,
        length_in=_clean_text(length_in, 20),
        texture=_clean_text(texture),
        color=_clean_text(color),
        origin=_clean_text(origin),
        hair_type=_clean_text(hair_type),
        unit=unit,
        package_weight_g=package_weight_g,
        package_price=package_price,
        price=price,
        cost=cost,
        min_stock=min_stock,
    )
    db.add(variant)
    db.flush()
    remember_price(db, variant, user)
    remember_cost(db, variant, user, "alta de variante")
    if unit == "unidad" and initial_qty:
        apply_stock(
            db, variant, "unidad", initial_qty, 0, "apertura", cost, user,
            ref_type="variant", ref_id=variant.id, note="Inventario inicial",
        )
    if unit == "gramo" and initial_qty:
        apply_stock(
            db, variant, "paquete", initial_qty, 0, "apertura", package_or_unit_cost, user,
            ref_type="variant", ref_id=variant.id, note="Paquetes iniciales",
        )
    if unit == "gramo" and initial_grams:
        apply_stock(
            db, variant, "gramo", initial_grams, 0, "apertura", cost, user,
            ref_type="variant", ref_id=variant.id, note="Gramos sueltos iniciales",
        )
    audit(db, user, "crear_variante", "variant", variant.id, variant.sku)
    return variant


def update_product(
    db: Session,
    user: User,
    product: Product,
    *,
    name: str,
    category: str,
    description: str,
    returnable: bool,
    retention_percent: int,
    quick_sale: bool,
    active: bool,
) -> None:
    if not user.is_admin:
        raise BusinessError("Solo administración edita el catálogo.")
    clean_name = (name or "").strip()
    if not clean_name:
        raise BusinessError("El producto necesita un nombre.")
    if retention_percent < 0 or retention_percent > 100:
        raise BusinessError("La retención del separado va de 0 a 100%.")
    product.name = clean_name[:160]
    product.category_id = category_by_name(db, category).id
    product.description = (description or "").strip()
    product.returnable = returnable
    product.retention_percent = retention_percent
    product.quick_sale = quick_sale
    product.active = active
    audit(db, user, "editar_producto", "product", product.id, product.code)


def update_variant(
    db: Session,
    user: User,
    variant: Variant,
    *,
    length_in: str,
    texture: str,
    color: str,
    origin: str,
    hair_type: str,
    min_stock: int,
    active: bool,
) -> None:
    if not user.is_admin:
        raise BusinessError("Solo administración edita variantes.")
    if min_stock < 0:
        raise BusinessError("El stock mínimo no puede ser negativo.")
    variant.length_in = _clean_text(length_in, 20)
    variant.texture = _clean_text(texture)
    variant.color = _clean_text(color)
    variant.origin = _clean_text(origin)
    variant.hair_type = _clean_text(hair_type)
    variant.min_stock = min_stock
    variant.active = active
    audit(db, user, "editar_variante", "variant", variant.id, variant.sku)


def set_price(
    db: Session,
    user: User,
    variant: Variant,
    *,
    price: int,
    package_price: int | None = None,
) -> None:
    if not user.is_admin:
        raise BusinessError("Solo administración cambia precios.")
    if price < 0 or (package_price is not None and package_price < 0):
        raise BusinessError("El precio no puede ser negativo.")
    changed = price != variant.price or (
        package_price is not None and package_price != variant.package_price
    )
    variant.price = price
    if package_price is not None and variant.unit == "gramo":
        variant.package_price = package_price
    if changed:
        remember_price(db, variant, user)
        audit(db, user, "cambiar_precio", "variant", variant.id, variant.sku)


def correct_cost(
    db: Session,
    variant: Variant,
    new_cost: int,
    reason: str,
    actor: User,
    username: str,
    password: str,
) -> None:
    authorizer = authorize(db, actor, username, password)
    if new_cost < 0:
        raise BusinessError("El costo no puede ser negativo.")
    if not (reason or "").strip():
        raise BusinessError("Escribe el motivo de la corrección de costo.")
    previous = variant.cost
    variant.cost = new_cost
    remember_cost(db, variant, authorizer, f"corrección: {reason.strip()} (antes {previous})")
    audit(db, authorizer, "corregir_costo", "variant", variant.id, reason.strip())


def open_package(db: Session, user: User, variant: Variant, note: str = "") -> None:
    if variant.unit != "gramo":
        raise BusinessError("Esta variante no se vende por gramos.")
    if variant.package_weight_g <= 0:
        raise BusinessError("La variante no tiene el peso fijo del paquete.")
    if available(variant, "paquete") < 1:
        raise BusinessError("No hay un paquete sellado disponible para abrir.")
    weight = variant.package_weight_g
    apply_stock(
        db, variant, "paquete", -1, 0, "apertura_paquete", bucket_cost(variant, "paquete"), user,
        ref_type="variant", ref_id=variant.id, note=note or "Apertura de paquete",
    )
    apply_stock(
        db, variant, "gramo", weight, 0, "apertura_paquete", variant.cost, user,
        ref_type="variant", ref_id=variant.id, note=f"Entraron {weight} g",
    )
    audit(db, user, "abrir_paquete", "variant", variant.id, variant.sku)


def adjust_stock(
    db: Session,
    variant: Variant,
    bucket: str,
    delta: int,
    reason: str,
    actor: User,
    username: str,
    password: str,
) -> None:
    authorizer = authorize(db, actor, username, password)
    if delta == 0:
        raise BusinessError("El ajuste no puede ser cero.")
    if not (reason or "").strip():
        raise BusinessError("El ajuste necesita un motivo.")
    if bucket == "gramo" and variant.unit != "gramo":
        raise BusinessError("Esta variante no maneja gramos.")
    if bucket == "paquete" and variant.unit != "gramo":
        raise BusinessError("Esta variante no maneja paquetes.")
    if bucket == "unidad" and variant.unit != "unidad":
        raise BusinessError("Esta variante se controla por paquetes y gramos.")
    apply_stock(
        db, variant, bucket, delta, 0, "ajuste", bucket_cost(variant, bucket) if bucket != "paquete" else bucket_cost(variant, "paquete"),
        authorizer, ref_type="variant", ref_id=variant.id, note=reason.strip(),
    )
    audit(db, authorizer, "ajuste_inventario", "variant", variant.id, f"{bucket} {delta:+d}: {reason.strip()}")


def mark_unsellable(
    db: Session,
    variant: Variant,
    bucket: str,
    qty: int,
    reason: str,
    actor: User,
    username: str,
    password: str,
) -> None:
    authorizer = authorize(db, actor, username, password)
    if qty <= 0:
        raise BusinessError("Indica cuántas piezas pasan a no vendible.")
    if not (reason or "").strip():
        raise BusinessError("Indica por qué la mercancía no se puede vender.")
    if available(variant, bucket) < qty:
        raise BusinessError("Solo se puede marcar como no vendible lo que está disponible, no lo reservado.")
    cost = bucket_cost(variant, bucket)
    apply_stock(
        db, variant, bucket, -qty, 0, "no_vendible", cost, authorizer,
        ref_type="variant", ref_id=variant.id, note=reason.strip(),
    )
    if bucket == "unidad":
        variant.unsellable_units += qty
    elif bucket == "paquete":
        variant.unsellable_packages += qty
    elif bucket == "gramo":
        variant.unsellable_grams += qty
    audit(db, authorizer, "no_vendible", "variant", variant.id, f"{bucket} {qty}: {reason.strip()}")


def variant_state(variant: Variant) -> dict:
    if variant.unit == "unidad":
        avail = variant.stock_units - variant.reserved_units
        agotado = avail <= 0
        bajo = avail <= variant.min_stock
        return {
            "available": avail,
            "reserved": variant.reserved_units,
            "unsellable": variant.unsellable_units,
            "packages": 0,
            "grams": 0,
            "agotado": agotado,
            "bajo": bajo,
        }
    avail_pkg = variant.stock_packages - variant.reserved_packages
    agotado = avail_pkg <= 0 and variant.stock_grams <= 0
    bajo = avail_pkg <= variant.min_stock
    return {
        "available": avail_pkg,
        "reserved": variant.reserved_packages,
        "unsellable": variant.unsellable_packages,
        "packages": avail_pkg,
        "grams": variant.stock_grams,
        "agotado": agotado,
        "bajo": bajo,
    }


def low_stock(db: Session) -> list[Variant]:
    variants = db.scalars(
        select(Variant).join(Product).where(Variant.active.is_(True), Product.active.is_(True)).order_by(Product.name)
    ).all()
    return [variant for variant in variants if variant_state(variant)["bajo"]]


def apply_import_rows(db: Session, user: User, rows: list[dict]) -> int:
    """Crea productos y, si vienen datos de reserva, deja el separado armado."""
    from app.logic.sales import create_layaway, save_customer

    count = 0
    for row in rows:
        code = norm_code(row["codigo"], "El código")
        product = db.scalar(select(Product).where(Product.code == code))
        if product is None:
            product = create_product(
                db, user,
                code=code,
                name=row["nombre"],
                category=row["categoria"],
                description=row.get("descripcion") or "",
                returnable=bool(row.get("devolvible")),
                retention_percent=int(row.get("retencion_pct") or 0),
                quick_sale=bool(row.get("venta_rapida")),
                sku=row["sku"],
                length_in=row.get("largo_pulgadas") or "",
                texture=row.get("textura") or "",
                color=row.get("color") or "",
                origin=row.get("origen") or "",
                hair_type=row.get("tipo_cabello") or "",
                unit=row["unidad"],
                package_weight_g=int(row.get("peso_paquete_g") or 0),
                package_price=int(row.get("precio_paquete") or 0),
                price=int(row.get("precio_venta") or 0),
                package_or_unit_cost=int(row.get("costo") or 0),
                initial_qty=int(row.get("cantidad") or 0),
                initial_grams=int(row.get("gramos_sueltos") or 0),
                min_stock=int(row.get("stock_minimo") or 0),
            )
            variant = product.variants[-1]
        else:
            if fold_name(product.name) != fold_name(row["nombre"]):
                raise BusinessError(
                    f"El código {code} ya es «{product.name}» y la plantilla dice «{row['nombre']}»."
                )
            variant = add_variant(
                db, user, product,
                sku=row["sku"],
                length_in=row.get("largo_pulgadas") or "",
                texture=row.get("textura") or "",
                color=row.get("color") or "",
                origin=row.get("origen") or "",
                hair_type=row.get("tipo_cabello") or "",
                unit=row["unidad"],
                package_weight_g=int(row.get("peso_paquete_g") or 0),
                package_price=int(row.get("precio_paquete") or 0),
                price=int(row.get("precio_venta") or 0),
                package_or_unit_cost=int(row.get("costo") or 0),
                initial_qty=int(row.get("cantidad") or 0),
                initial_grams=int(row.get("gramos_sueltos") or 0),
                min_stock=int(row.get("stock_minimo") or 0),
            )
        reserved_qty = int(row.get("reservado_cantidad") or 0)
        reserved_name = (row.get("reservado_cliente") or "").strip()
        if reserved_name or reserved_qty:
            if not reserved_name:
                raise BusinessError(f"La fila {row.get('fila')} reserva mercancía sin nombre de clienta.")
            if reserved_qty <= 0:
                reserved_qty = int(row.get("cantidad") or 0)
            customer = save_customer(
                db, user,
                name=reserved_name,
                phone=row.get("reservado_telefono") or "",
                doc_id=row.get("reservado_documento") or "",
                email="",
                notes="Creada en la carga de inventario.",
                consent=bool(row.get("reservado_autoriza")),
                customer_id=None,
            )
            bucket = "unidad" if variant.unit == "unidad" else "paquete"
            create_layaway(
                db, user,
                customer_id=customer.id,
                variant_id=variant.id,
                bucket=bucket,
                qty=reserved_qty,
                agreed_price=int(row.get("precio_venta") or 0) * reserved_qty
                if variant.unit == "unidad"
                else int(row.get("precio_paquete") or 0) * reserved_qty,
                deposit=int(row.get("reservado_abonado") or 0),
                method=row.get("reservado_medio") or "efectivo",
                idempotency_key=f"import-{row.get('fila')}-{variant.sku}",
                initial_load=True,
                account_code=row.get("reservado_cuenta") or None,
                note="Separado cargado con el inventario inicial.",
            )
        count += 1
    return count


def fold_name(value: str) -> str:
    return " ".join((value or "").lower().split())


def movement_count(db: Session, product: Product) -> int:
    variant_ids = [variant.id for variant in product.variants]
    if not variant_ids:
        return 0
    return int(
        db.scalar(select(func.count()).select_from(StockMove).where(StockMove.variant_id.in_(variant_ids))) or 0
    )
