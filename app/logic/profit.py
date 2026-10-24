"""Ganancia estimada. Los anticipos, traslados y retiros no entran."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.logic.caja import find_open_session
from app.logic.catalog import low_stock, variant_state
from app.logic.common import setting_int
from app.logic.sales import committed_advances, days_open, layaway_state
from app.models import Account, Expense, Layaway, Payment, Purchase, Sale, StockMove
from app.utils import as_utc, period_bounds, today_bogota


def profit_report(db: Session, start: date, end: date) -> dict:
    opening, closing = period_bounds(start, end)
    sales = db.scalars(
        select(Sale)
        .where(Sale.created_at >= opening, Sale.created_at <= closing, Sale.status != "anulada")
        .order_by(Sale.created_at)
    ).all()
    net_sales = 0
    cogs = 0
    discounts = 0
    returned_revenue = 0
    operations = []
    for sale in sales:
        sale_revenue = 0
        sale_cogs = 0
        for line in sale.lines:
            if line.qty <= 0:
                continue
            left = line.qty - line.returned_stock - line.returned_unsellable
            sale_revenue += (line.line_total * left) // line.qty
            # Lo devuelto al inventario revierte el costo. Lo no vendible se queda como costo.
            sale_cogs += line.unit_cost * (line.qty - line.returned_stock)
            returned_revenue += (line.line_total * line.returned) // line.qty
        net_sales += sale_revenue
        cogs += sale_cogs
        discounts += sale.discount_amount
        operations.append({
            "kind": "venta",
            "ref": sale.number,
            "when": sale.created_at,
            "detail": "Entrega de separado" if sale.source == "separado" else "Venta",
            "amount": sale_revenue,
            "cost": sale_cogs,
        })
    gross = net_sales - cogs
    merma_moves = db.scalars(
        select(StockMove).where(
            StockMove.reason == "no_vendible",
            StockMove.created_at >= opening,
            StockMove.created_at <= closing,
        )
    ).all()
    merma = sum(abs(move.qty) * move.unit_cost for move in merma_moves)
    expenses = db.scalars(
        select(Expense).where(
            Expense.status == "pagado",
            Expense.spent_on >= start,
            Expense.spent_on <= end,
        )
    ).all()
    payroll = sum(item.amount for item in expenses if item.category.slug == "nomina")
    other_expenses = sum(item.amount for item in expenses if item.category.slug != "nomina")
    for item in expenses:
        operations.append({
            "kind": "gasto",
            "ref": item.category.name,
            "when": item.paid_at,
            "detail": item.description,
            "amount": -item.amount,
            "cost": 0,
        })
    retentions = int(
        db.scalar(
            select(func.coalesce(func.sum(Layaway.retention_amount), 0)).where(
                Layaway.status == "cancelado",
                Layaway.closed_at >= opening,
                Layaway.closed_at <= closing,
            )
        )
        or 0
    )
    net = gross + retentions - other_expenses - payroll - merma
    advances = _advances_excluded(db, opening, closing)
    balances = [
        {"code": account.code, "name": account.name, "kind": account.kind, "balance": account.balance}
        for account in db.scalars(select(Account).where(Account.active.is_(True)).order_by(Account.id)).all()
    ]
    return {
        "start": start,
        "end": end,
        "net_sales": net_sales,
        "discounts": discounts,
        "returned_revenue": returned_revenue,
        "cogs": cogs,
        "gross": gross,
        "merma": merma,
        "expenses": other_expenses,
        "payroll": payroll,
        "retentions": retentions,
        "net": net,
        "layaway_advances_excluded": advances,
        "committed_advances": committed_advances(db),
        "cash_on_hand": sum(item["balance"] for item in balances),
        "accounts": balances,
        "operations": operations,
        "sales_count": len(sales),
    }


def _advances_excluded(db: Session, opening, closing) -> int:
    payments = db.scalars(
        select(Payment).where(
            Payment.kind == "abono_separado",
            Payment.status == "confirmado",
            Payment.created_at >= opening,
            Payment.created_at <= closing,
        )
    ).all()
    total = 0
    for payment in payments:
        layaway = db.get(Layaway, payment.layaway_id) if payment.layaway_id else None
        if layaway is None:
            continue
        delivered_here = False
        if layaway.status == "entregado" and layaway.delivered_sale_id:
            sale = db.get(Sale, layaway.delivered_sale_id)
            sold_at = as_utc(sale.created_at) if sale else None
            if sold_at and opening <= sold_at <= closing:
                delivered_here = True
        if not delivered_here:
            total += payment.amount
    return total


def dashboard(db: Session, day: date | None = None) -> dict:
    day = day or today_bogota()
    report = profit_report(db, day, day)
    opening, closing = period_bounds(day, day)
    sales_today = db.scalars(
        select(Sale).where(Sale.created_at >= opening, Sale.created_at <= closing, Sale.status != "anulada")
    ).all()
    delivered_total = sum(sale.total - _returned_total(sale) for sale in sales_today)
    layaway_today = int(
        db.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.kind == "abono_separado",
                Payment.status == "confirmado",
                Payment.created_at >= opening,
                Payment.created_at <= closing,
            )
        )
        or 0
    )
    credit_today = int(
        db.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.kind == "abono_fiado",
                Payment.status == "confirmado",
                Payment.created_at >= opening,
                Payment.created_at <= closing,
            )
        )
        or 0
    )
    alert_days = setting_int(db, "layaway_alert_days")
    active = db.scalars(select(Layaway).where(Layaway.status == "activo").order_by(Layaway.due_at)).all()
    due = []
    for layaway in active:
        state = layaway_state(layaway, alert_days)
        if state in {"por_vencer", "vencido"}:
            due.append({"layaway": layaway, "state": state, "days": days_open(layaway)})
    receivables = int(
        db.scalar(select(func.coalesce(func.sum(Sale.balance), 0)).where(Sale.balance > 0, Sale.status != "anulada")) or 0
    )
    payables = int(
        db.scalar(
            select(func.coalesce(func.sum(Purchase.balance), 0)).where(
                Purchase.balance > 0, Purchase.status != "anulada"
            )
        )
        or 0
    )
    recent = db.scalars(select(Sale).order_by(Sale.created_at.desc()).limit(8)).all()
    accounts = db.scalars(select(Account).where(Account.active.is_(True)).order_by(Account.id)).all()
    alerts = []
    for variant in low_stock(db)[:8]:
        state = variant_state(variant)
        alerts.append({"variant": variant, "state": state})
    return {
        "day": day,
        "report": report,
        "delivered_total": delivered_total,
        "delivered_count": len(sales_today),
        "layaway_today": layaway_today,
        "credit_today": credit_today,
        "session": find_open_session(db),
        "accounts": list(accounts),
        "committed": committed_advances(db),
        "due_layaways": due[:6],
        "due_count": len(due),
        "sales_today": list(sales_today),
        "receivables": receivables,
        "payables": payables,
        "recent": list(recent),
        "alerts": alerts,
    }


def _returned_total(sale: Sale) -> int:
    total = 0
    for line in sale.lines:
        if line.qty:
            total += (line.line_total * line.returned) // line.qty
    return total
