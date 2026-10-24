"""Aplicación web: sesión, pantallas y errores."""

from __future__ import annotations

import os
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.backup import maybe_daily_backup
from app.db import DATA, build_engine, database_url as default_database_url, session_factory
from app.logic.common import BusinessError, find_open_session, setting
from app.logic.sales import AlreadyDone, empty_cart
from app.models import Base, User
from app.seed import ensure_settings, seed_demo
from app.utils import BOGOTA, MESES_LARGOS, METHODS, fmt_date, fmt_dt, fmt_long, money

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES))
templates.env.filters["money"] = money
templates.env.filters["dt"] = fmt_dt
templates.env.filters["fecha"] = fmt_date
templates.env.filters["largo"] = fmt_long
templates.env.globals["METHODS"] = METHODS

NAV = [
    ("/", "Inicio", "both"),
    ("/vender", "Vender", "both"),
    ("/caja", "Caja", "both"),
    ("/separados", "Separados", "both"),
    ("/fiados", "Fiados", "both"),
    ("/clientes", "Clientas", "both"),
    ("/productos", "Productos", "both"),
    ("/inventario", "Inventario", "both"),
    ("/cuentas", "Cuentas", "admin"),
    ("/compras", "Compras", "admin"),
    ("/gastos", "Gastos", "admin"),
    ("/ganancias", "Ganancias", "admin"),
    ("/reportes", "Reportes", "admin"),
    ("/usuarios", "Usuarios", "admin"),
    ("/ajustes", "Ajustes", "admin"),
    ("/auditoria", "Auditoría", "admin"),
    ("/respaldo", "Respaldo", "admin"),
]


def load_secret(persistent: bool) -> str:
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    if not persistent:
        return secrets.token_hex(32)
    path = DATA / "secret.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text().strip():
        return path.read_text().strip()
    key = secrets.token_hex(32)
    path.write_text(key)
    return key


def current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("uid")
    if not user_id:
        return None
    user = db.get(User, user_id)
    if user is None or not user.active:
        request.session.pop("uid", None)
        return None
    return user


def ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def flash(request: Request, message: str, kind: str = "ok") -> None:
    bag = list(request.session.get("flashes") or [])
    bag.append({"message": message, "kind": kind})
    request.session["flashes"] = bag


def pop_flashes(request: Request) -> list[dict]:
    flashes = list(request.session.get("flashes") or [])
    if flashes:
        request.session["flashes"] = []
    return flashes


def get_db(request: Request):
    db = request.app.state.sessions()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = current_user(request, db)
    if user is None:
        nxt = request.url.path
        if request.url.query:
            nxt = f"{nxt}?{request.url.query}"
        raise See(f"/login?next={nxt}")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise BusinessError("Esta parte es solo para administración.", redirect="/")
    return user


class See(Exception):
    def __init__(self, url: str):
        self.url = url


def shelf_alerts(db: Session) -> dict:
    """Cuenta las alertas que ya calcula el inventario y los separados, para la campana."""
    from app.logic.catalog import low_stock, variant_state
    from app.logic.common import setting_int
    from app.logic.sales import layaway_state
    from app.models import Layaway

    agotados = bajos = 0
    for variant in low_stock(db):
        state = variant_state(variant)
        if state["agotado"]:
            agotados += 1
        elif state["bajo"]:
            bajos += 1
    alert_days = setting_int(db, "layaway_alert_days")
    due = 0
    active = db.scalars(select(Layaway).where(Layaway.status == "activo")).all()
    for layaway in active:
        if layaway_state(layaway, alert_days) in {"por_vencer", "vencido"}:
            due += 1
    return {
        "bell_agotados": agotados,
        "bell_bajos": bajos,
        "bell_due": due,
        "alert_count": agotados + bajos + due,
    }


def render(request: Request, db: Session, name: str, status: int = 200, *, partial: bool = False, **ctx):
    user = ctx.get("user", current_user(request, db))
    nav = [item for item in NAV if item[2] == "both" or (user and user.is_admin)]
    ctx.setdefault("user", user)
    now = datetime.now(BOGOTA)
    hour12 = now.hour % 12 or 12
    days = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    bells = shelf_alerts(db) if user and not partial else {
        "bell_agotados": 0, "bell_bajos": 0, "bell_due": 0, "alert_count": 0,
    }
    ctx.update({
        "request": request,
        "caja": find_open_session(db) if user else None,
        "flashes": [] if partial else pop_flashes(request),
        "csrf": ensure_csrf(request),
        "nav": nav,
        "business": setting(db, "business_name") if user or name == "login.html" else "KINKARA AFRO",
        "demo_hint": setting(db, "show_demo_hint") == "1",
        "legend": setting(db, "receipt_legend"),
        "clock_date": f"{days[now.weekday()]}, {now.day} de {MESES_LARGOS[now.month - 1]} de {now.year}",
        "clock_time": f"{hour12}:{now.minute:02d} {'a. m.' if now.hour < 12 else 'p. m.'}",
        **bells,
    })
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def redirect(url: str, request: Request | None = None, message: str | None = None, kind: str = "ok") -> RedirectResponse:
    if request and message:
        flash(request, message, kind)
    return RedirectResponse(url, status_code=303)


def cart_of(request: Request) -> dict:
    cart = request.session.get("cart")
    if not cart or "lines" not in cart:
        cart = empty_cart()
        request.session["cart"] = cart
    return cart


def save_cart(request: Request, cart: dict) -> None:
    request.session["cart"] = cart


def safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def create_app(database_url: str | None = None, *, seed: bool = True) -> FastAPI:
    DATA.mkdir(parents=True, exist_ok=True)
    url = database_url or default_database_url()
    engine = build_engine(url)
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    if seed:
        with factory() as db:
            if db.scalar(select(func.count()).select_from(User)) == 0:
                seed_demo(db)
            else:
                ensure_settings(db)
            db.commit()
    maybe_daily_backup(engine)
    app = FastAPI(title="KINKARA AFRO")
    app.state.engine = engine
    app.state.sessions = factory
    app.state.database_url = url
    from starlette.middleware.sessions import SessionMiddleware

    persistent = url.endswith("kinkara.db")
    app.add_middleware(
        SessionMiddleware,
        secret_key=load_secret(persistent),
        max_age=60 * 60 * 16,
        same_site="lax",
        https_only=False,
        session_cookie="kinkara",
    )
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    from app.pages import router

    app.include_router(router)

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(See)
    async def _see(request: Request, exc: See):
        return RedirectResponse(exc.url, status_code=303)

    @app.exception_handler(BusinessError)
    async def _business(request: Request, exc: BusinessError):
        flash(request, exc.message, "error")
        target = exc.redirect
        if not target:
            referer = request.headers.get("referer") or ""
            base = str(request.base_url)
            target = referer if referer.startswith(base) else "/"
        return RedirectResponse(target, status_code=303)

    @app.exception_handler(AlreadyDone)
    async def _done(request: Request, exc: AlreadyDone):
        paths = {
            "sale": f"/ventas/{exc.ref_id}",
            "layaway": f"/separados/{exc.ref_id}",
            "purchase": f"/compras/{exc.ref_id}",
        }
        if exc.ref_type == "sale":
            request.session["cart"] = empty_cart()
        flash(request, "Esa operación ya estaba registrada. No se duplicó.", "info")
        return RedirectResponse(paths.get(exc.ref_type, "/"), status_code=303)

    @app.exception_handler(404)
    async def _missing(request: Request, _exc):
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><title>No encontrado</title>"
            "<body style='font-family:sans-serif;background:#f3efe6;color:#241c16;padding:3rem'>"
            "<h1>Esa página no está.</h1><p><a href='/'>Volver al inicio</a></p></body>",
            status_code=404,
        )

    return app
