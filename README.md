# KINKARA AFRO

Caja, inventario, separados y fiados del salón. Una sola caja compartida, pesos enteros y la fecha de Bogotá. El sistema distingue el dinero que entra de la ganancia: un abono de separado no es una venta.

La hoja del 13 de octubre de 2026 contaba $192.000 en efectivo como si fueran utilidad. $150.000 eran anticipos de separados y solo $42.000 eran ventas entregadas. El cierre y el reporte de ganancias no vuelven a mezclar esas cifras.

## Cómo correrlo

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm install
npm run css
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8741
```

Abre [http://127.0.0.1:8741](http://127.0.0.1:8741). La primera vez crea `data/kinkara.db`, las cuentas y un catálogo de demostración.

Usuarios de la semilla, visibles en el login mientras Ajustes tenga el recuadro de primer acceso:

| Usuario | Contraseña | Rol |
| --- | --- | --- |
| admin | Afro.2026 | Administración |
| caja | Caja.2026 | Caja |

Cámbialas en Usuarios y oculta el recuadro en Ajustes.

Pruebas: `.venv/bin/pytest`

## Qué hay al entrar

- Caja en $66.500, lista para abrir el turno.
- Ahorro en $100.000. Ese dinero es el anticipo de María José Rentería (peluca 22″ loose wave 1B, acordada en $390.000, abonados $100.000). El separado se creó hace 25 días y vence en 5. La reserva no está disponible para vender.
- Unos 16 SKU: pelucas, cabello natural y sintético, una referencia por gramos y accesorios de precio fijo. El gel está agotado y el cepillo está bajo el mínimo.

Para repetir la hoja del 13 de octubre encima de esa apertura:

1. Abre la caja contando $66.500.
2. Cobra una venta entregada de $42.000 en efectivo.
3. Recibe un abono de separado de $150.000 en efectivo.
4. Cierra contando $258.500 y traslada $200.000 a ahorro.

El esperado queda en $258.500, la diferencia en $0 y la base de mañana en $58.500. La ganancia del día es la venta de $42.000 menos su costo. Los $150.000 siguen siendo anticipo. El traslado no cambia el dinero total. Ahorro queda en $300.000 porque ya traía los $100.000 de María José.

## Cuentas y reglas que ya vienen decididas

- Medios: efectivo, Nequi, Daviplata, transferencia Bancolombia, Bre-B y tarjeta. Bre-B no es una cuenta: acredita la cuenta configurada, por defecto Bancolombia.
- El descuento de caja llega al 10% solo si el subtotal es de al menos $150.000. Otro descuento pide clave de administración.
- Un separado reserva la unidad o el paquete, no gramos sueltos. El abono inicial mínimo es el 30%; administración puede bajarlo. Vence a los 30 días y no se cancela solo: queda vencido y reservado. Cancelarlo devuelve lo pagado menos la retención del producto (pelucas 20% en la semilla). La retención es otro ingreso, no una venta.
- La mercancía se puede vender por paquete y por gramo. Abrir un paquete resta un paquete sellado y suma los gramos.
- Un producto con movimientos no se borra: se desactiva. Las ventas, pagos y cierres se anulan; no se eliminan.
- La nómina por turno queda para más adelante. La categoría Nómina existe para que el cierre la muestre si se registra un pago.
- No hay factura electrónica. El comprobante dice que no es válido como factura.

## PostgreSQL

Sin `DATABASE_URL` usa SQLite en `data/kinkara.db`. Para Postgres:

```bash
DATABASE_URL=postgresql+psycopg://usuario:clave@localhost:5432/kinkara .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8741
```

## Copia de seguridad

Con la base SQLite de `data/kinkara.db`, cada día se guarda una copia en `data/backups/` y se conservan 30. Desde Respaldo se descarga y se restaura escribiendo `RESTAURAR`.

Para sacar la copia del computador, `scripts/backup.sh` hace el dump (SQLite o `pg_dump` si `DATABASE_URL` es Postgres) y, si defines `RCLONE_REMOTE`, lo sube con rclone. La aplicación no guarda credenciales de Drive.

## Teléfono

Es una PWA: el manifiesto y el ícono permiten instalarla. Las pantallas piden red. Sin conexión se muestra un aviso y no se envían formularios. El service worker solo guarda estáticos y la página sin conexión.
