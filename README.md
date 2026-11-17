# Roger Store 🌿

Магазин графики для эндуро-мотоциклов в Telegram (aiogram 3 + SQLite).

## Запуск
```bash
cd roger-store
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env     # впиши SHOP_TOKEN (и ADMIN_IDS)
.venv/bin/python bot.py
```
База `shop.db` создаётся сама, категории (Full Graphics Kit, Number Plates, Frame Protection,
Fork Guards, Swingarm, Custom) заводятся при первом запуске.

## Владелец
`/admin` или кнопка «🛠 Админ-панель»: товары (с несколькими фото), категории, заказы
со сменой статуса, реквизиты (несколько, вкл/выкл), рассылка. Первым делом добавь реквизиты.

## Статусы заказа
Новый → Ожидает оплаты → Оплачен → В обработке → Отправлен → Завершён.
Заказ создаётся «Новым», после «Я оплатил» становится «Ожидает оплаты» и уходит владельцу на проверку.
Клиент получает сообщение при каждой смене статуса.
