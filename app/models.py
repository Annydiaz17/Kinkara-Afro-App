"""Tablas del sistema. Los movimientos de dinero y de inventario no se borran."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.utils import utcnow


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="cajero")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def role_label(self) -> str:
        return "Administración" if self.is_admin else "Caja"


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Counter(Base):
    __tablename__ = "counters"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class Idempotency(Base):
    __tablename__ = "idempotency"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    scope: Mapped[str] = mapped_column(String(40))
    ref_type: Mapped[str] = mapped_column(String(40), default="")
    ref_id: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(80))
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    user: Mapped[User | None] = relationship()


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    description: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    returnable: Mapped[bool] = mapped_column(Boolean, default=False)
    retention_percent: Mapped[int] = mapped_column(Integer, default=20)
    quick_sale: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    category: Mapped[Category] = relationship()
    variants: Mapped[list[Variant]] = relationship(back_populates="product", cascade="all, delete-orphan")


class Variant(Base):
    __tablename__ = "variants"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    sku: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    length_in: Mapped[str] = mapped_column(String(20), default="")
    texture: Mapped[str] = mapped_column(String(40), default="")
    color: Mapped[str] = mapped_column(String(40), default="")
    origin: Mapped[str] = mapped_column(String(40), default="")
    hair_type: Mapped[str] = mapped_column(String(40), default="")
    unit: Mapped[str] = mapped_column(String(20))  # unidad | gramo
    package_weight_g: Mapped[int] = mapped_column(Integer, default=0)
    package_price: Mapped[int] = mapped_column(BigInteger, default=0)
    price: Mapped[int] = mapped_column(BigInteger, default=0)
    cost: Mapped[int] = mapped_column(BigInteger, default=0)
    stock_units: Mapped[int] = mapped_column(Integer, default=0)
    stock_packages: Mapped[int] = mapped_column(Integer, default=0)
    stock_grams: Mapped[int] = mapped_column(Integer, default=0)
    reserved_units: Mapped[int] = mapped_column(Integer, default=0)
    reserved_packages: Mapped[int] = mapped_column(Integer, default=0)
    unsellable_units: Mapped[int] = mapped_column(Integer, default=0)
    unsellable_packages: Mapped[int] = mapped_column(Integer, default=0)
    unsellable_grams: Mapped[int] = mapped_column(Integer, default=0)
    min_stock: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    product: Mapped[Product] = relationship(back_populates="variants")

    @property
    def label(self) -> str:
        bits = [self.product.name]
        if self.length_in:
            bits.append(f'{self.length_in}″')
        for part in (self.texture, self.color, self.origin, self.hair_type):
            if part:
                bits.append(part)
        return " · ".join(bits)

    @property
    def short(self) -> str:
        bits = []
        if self.length_in:
            bits.append(f'{self.length_in}″')
        if self.texture:
            bits.append(self.texture)
        if self.color:
            bits.append(self.color)
        return " · ".join(bits) if bits else self.sku


class PriceHistory(Base):
    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"), index=True)
    price: Mapped[int] = mapped_column(BigInteger)
    package_price: Mapped[int] = mapped_column(BigInteger, default=0)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User | None] = relationship()


class CostHistory(Base):
    __tablename__ = "cost_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"), index=True)
    cost: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(200), default="")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User | None] = relationship()


class StockMove(Base):
    __tablename__ = "stock_moves"

    id: Mapped[int] = mapped_column(primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"), index=True)
    bucket: Mapped[str] = mapped_column(String(20))
    qty: Mapped[int] = mapped_column(Integer, default=0)
    reserved_delta: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(40))
    unit_cost: Mapped[int] = mapped_column(BigInteger, default=0)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    ref_type: Mapped[str] = mapped_column(String(40), default="")
    ref_id: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    variant: Mapped[Variant] = relationship()
    user: Mapped[User | None] = relationship()


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(20))
    balance: Mapped[int] = mapped_column(BigInteger, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    system: Mapped[bool] = mapped_column(Boolean, default=False)

    moves: Mapped[list[AccountMove]] = relationship(back_populates="account")


class AccountMove(Base):
    __tablename__ = "account_moves"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(40))
    ref_type: Mapped[str] = mapped_column(String(40), default="")
    ref_id: Mapped[int] = mapped_column(Integer, default=0)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("cash_sessions.id"))
    note: Mapped[str] = mapped_column(Text, default="")
    voided: Mapped[bool] = mapped_column(Boolean, default=False)
    void_reason: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    account: Mapped[Account] = relationship(back_populates="moves")
    user: Mapped[User | None] = relationship()


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(40), default="")
    doc_id: Mapped[str] = mapped_column(String(40), default="")
    email: Mapped[str] = mapped_column(String(120), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    consent: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CashSession(Base):
    __tablename__ = "cash_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="abierta", index=True)
    opening_base: Mapped[int] = mapped_column(BigInteger, default=0)
    counted_open: Mapped[int] = mapped_column(BigInteger, default=0)
    open_diff: Mapped[int] = mapped_column(BigInteger, default=0)
    open_note: Mapped[str] = mapped_column(Text, default="")
    opened_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expected: Mapped[int | None] = mapped_column(BigInteger)
    book_before_close: Mapped[int | None] = mapped_column(BigInteger)
    counted_close: Mapped[int | None] = mapped_column(BigInteger)
    close_diff: Mapped[int | None] = mapped_column(BigInteger)
    close_note: Mapped[str] = mapped_column(Text, default="")
    transfer_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    transfer_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    closing_base: Mapped[int | None] = mapped_column(BigInteger)
    closed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    correction_note: Mapped[str] = mapped_column(Text, default="")

    opened_by: Mapped[User] = relationship(foreign_keys=[opened_by_id])
    closed_by: Mapped[User | None] = relationship(foreign_keys=[closed_by_id])
    transfer_account: Mapped[Account | None] = relationship()


class Sale(Base):
    __tablename__ = "sales"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("cash_sessions.id"))
    source: Mapped[str] = mapped_column(String(20), default="pos")
    layaway_id: Mapped[int | None] = mapped_column(Integer)
    subtotal: Mapped[int] = mapped_column(BigInteger, default=0)
    discount_pct: Mapped[int] = mapped_column(Integer, default=0)
    discount_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    discount_auth_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    total: Mapped[int] = mapped_column(BigInteger, default=0)
    paid: Mapped[int] = mapped_column(BigInteger, default=0)
    balance: Mapped[int] = mapped_column(BigInteger, default=0)
    due_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="confirmada", index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    customer: Mapped[Customer | None] = relationship()
    user: Mapped[User] = relationship(foreign_keys=[user_id])
    lines: Mapped[list[SaleLine]] = relationship(back_populates="sale", cascade="all, delete-orphan")
    payments: Mapped[list[Payment]] = relationship(back_populates="sale")


class SaleLine(Base):
    __tablename__ = "sale_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id"), index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"))
    bucket: Mapped[str] = mapped_column(String(20))
    qty: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[int] = mapped_column(BigInteger)
    unit_cost: Mapped[int] = mapped_column(BigInteger)
    line_total: Mapped[int] = mapped_column(BigInteger)
    returned_stock: Mapped[int] = mapped_column(Integer, default=0)
    returned_unsellable: Mapped[int] = mapped_column(Integer, default=0)

    sale: Mapped[Sale] = relationship(back_populates="lines")
    variant: Mapped[Variant] = relationship()

    @property
    def returned(self) -> int:
        return self.returned_stock + self.returned_unsellable

    @property
    def remaining(self) -> int:
        return self.qty - self.returned


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int | None] = mapped_column(ForeignKey("sales.id"), index=True)
    layaway_id: Mapped[int | None] = mapped_column(ForeignKey("layaways.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    method: Mapped[str] = mapped_column(String(30))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    amount: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="confirmado")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("cash_sessions.id"))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    sale: Mapped[Sale | None] = relationship(back_populates="payments")
    account: Mapped[Account | None] = relationship()
    user: Mapped[User | None] = relationship()


class Layaway(Base):
    __tablename__ = "layaways"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"))
    bucket: Mapped[str] = mapped_column(String(20))
    qty: Mapped[int] = mapped_column(Integer)
    agreed_price: Mapped[int] = mapped_column(BigInteger)
    paid: Mapped[int] = mapped_column(BigInteger, default=0)
    retention_percent: Mapped[int] = mapped_column(Integer, default=0)
    retention_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    refund_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    refund_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    status: Mapped[str] = mapped_column(String(20), default="activo", index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivered_sale_id: Mapped[int | None] = mapped_column(ForeignKey("sales.id"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    cancel_reason: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    customer: Mapped[Customer] = relationship()
    variant: Mapped[Variant] = relationship()
    user: Mapped[User | None] = relationship()
    payments: Mapped[list[Payment]] = relationship(foreign_keys="Payment.layaway_id", viewonly=True)

    @property
    def balance(self) -> int:
        return max(self.agreed_price - self.paid, 0)


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(40), default="")
    email: Mapped[str] = mapped_column(String(120), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Purchase(Base):
    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pedida")
    merchandise: Mapped[int] = mapped_column(BigInteger, default=0)
    shipping: Mapped[int] = mapped_column(BigInteger, default=0)
    total: Mapped[int] = mapped_column(BigInteger, default=0)
    paid: Mapped[int] = mapped_column(BigInteger, default=0)
    balance: Mapped[int] = mapped_column(BigInteger, default=0)
    ordered_at: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    supplier: Mapped[Supplier] = relationship()
    user: Mapped[User | None] = relationship()
    lines: Mapped[list[PurchaseLine]] = relationship(back_populates="purchase", cascade="all, delete-orphan")
    payments: Mapped[list[SupplierPayment]] = relationship(back_populates="purchase")


class PurchaseLine(Base):
    __tablename__ = "purchase_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_id: Mapped[int] = mapped_column(ForeignKey("purchases.id"), index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"))
    bucket: Mapped[str] = mapped_column(String(20))
    qty: Mapped[int] = mapped_column(Integer)
    unit_cost: Mapped[int] = mapped_column(BigInteger)
    freight_share: Mapped[int] = mapped_column(BigInteger, default=0)
    final_unit_cost: Mapped[int] = mapped_column(BigInteger, default=0)

    purchase: Mapped[Purchase] = relationship(back_populates="lines")
    variant: Mapped[Variant] = relationship()


class SupplierPayment(Base):
    __tablename__ = "supplier_payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_id: Mapped[int] = mapped_column(ForeignKey("purchases.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    amount: Mapped[int] = mapped_column(BigInteger)
    method: Mapped[str] = mapped_column(String(30))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("cash_sessions.id"))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    purchase: Mapped[Purchase] = relationship(back_populates="payments")
    account: Mapped[Account] = relationship()
    user: Mapped[User | None] = relationship()


class ExpenseCategory(Base):
    __tablename__ = "expense_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("expense_categories.id"))
    amount: Mapped[int] = mapped_column(BigInteger)
    spent_on: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default="pendiente")
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attachment: Mapped[str] = mapped_column(String(200), default="")
    session_id: Mapped[int | None] = mapped_column(ForeignKey("cash_sessions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    category: Mapped[ExpenseCategory] = relationship()
    user: Mapped[User | None] = relationship()
    account: Mapped[Account | None] = relationship()


class Reconciliation(Base):
    __tablename__ = "reconciliations"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    statement_balance: Mapped[int] = mapped_column(BigInteger)
    book_balance: Mapped[int] = mapped_column(BigInteger)
    difference: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text, default="")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account] = relationship()
    user: Mapped[User | None] = relationship()


class ImportBatch(Base):
    __tablename__ = "import_batches"
    __table_args__ = (UniqueConstraint("file_hash", name="uq_import_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    file_hash: Mapped[str] = mapped_column(String(80))
    rows: Mapped[int] = mapped_column(Integer, default=0)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    forced: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BackupRun(Base):
    __tablename__ = "backup_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String(240))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    note: Mapped[str] = mapped_column(String(200), default="")
