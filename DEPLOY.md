# Деплой и обслуживание

Бот работает на сервере как user-сервис systemd `tutor-bot`, код лежит в `~/tutor_bot`.
Код пишется и проверяется локально, сервер только забирает его из GitHub.

## Первая установка на сервер

```bash
git clone git@github.com:mikefromru/urokoved_bot.git ~/tutor_bot
cd ~/tutor_bot
python3 -m venv venv
venv/bin/pip install -r requirements.txt
nano .env            # BOT_TOKEN=... и ADMIN_ID=...
sudo apt update && sudo apt install --only-upgrade tzdata
```

Юнит `~/.config/systemd/user/tutor-bot.service`:

```ini
[Unit]
Description=Urokoved — Telegram-бот для репетиторов
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=%h/tutor_bot
ExecStart=%h/tutor_bot/venv/bin/python bot.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now tutor-bot
loginctl enable-linger $USER     # чтобы бот работал без входа в систему и после перезагрузки
```

## Обновление

На своём компьютере: проверить на тестовом боте → `git push`. Потом на сервере:

```bash
cd ~/tutor_bot
git pull
venv/bin/pip install -r requirements.txt   # только если менялся requirements.txt
systemctl --user restart tutor-bot
```

## Управление сервисом

```bash
systemctl --user status tutor-bot      # работает ли
systemctl --user restart tutor-bot     # перезапустить (после обновления файлов)
systemctl --user stop tutor-bot        # остановить
systemctl --user start tutor-bot       # запустить
```

## Логи

```bash
journalctl --user -u tutor-bot -f        # вывод сервиса в реальном времени
journalctl --user -u tutor-bot -n 50     # последние 50 строк (если сервис упал)
tail -f ~/tutor_bot/logs/bot.log         # лог действий пользователей
grep "id=123456789" logs/bot.log         # всё, что делал конкретный пользователь
grep -A20 "ОШИБКА" logs/bot.log          # ошибки с подробностями
```

## Бэкапы

Делаются сами каждый день в 03:30 в `backups/` и приходят владельцу в Telegram.
Вручную: команда `/backup` в боте. Статистика: `/stats`.

Восстановление:

```bash
systemctl --user stop tutor-bot
cp tutor.db tutor.db.broken
gunzip -c backups/tutor-ГГГГ-ММ-ДД.db.gz > tutor.db
systemctl --user start tutor-bot
```
