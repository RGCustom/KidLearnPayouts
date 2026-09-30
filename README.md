# KidLearnPayouts

Учёт по Договору № 1/2026 — накопления, выплаты и статистика в тёмном веб-интерфейсе.
Просмотр, история и статистика открыты всем, **записи, выплаты и удаление — только по паролю**.

## Установка на Unraid

1. Дождитесь зелёной галочки в **Actions** (образ собирается автоматически при каждом коммите в `main`).
2. В терминале Unraid скачайте шаблон:
   ```
   wget -O /boot/config/plugins/dockerMan/templates-user/my-KidLearnPayouts.xml \
     https://raw.githubusercontent.com/RGCustom/KidLearnPayouts/main/kidlearnpayouts.xml
   ```
3. **Docker → Add Container →** в выпадающем списке шаблонов выберите **KidLearnPayouts**.
   Задайте пароль (`ADMIN_PASSWORD`), проверьте часовой пояс (`TZ`) и порт → **Apply**.
4. Откройте `http://IP-сервера:8090`.

Обновление: после нового коммита дождитесь Actions, затем на вкладке Docker → *Check for updates* / *Force update*.

Если Unraid пишет «pull access denied» — откройте Packages в профиле GitHub → `kidlearnpayouts` →
*Package settings* → *Change visibility* → **Public**.

## Без GitHub Actions (сборка прямо на сервере)
```
docker build -t kidlearnpayouts:latest https://github.com/RGCustom/KidLearnPayouts.git#main
```
и в шаблоне замените Repository на `kidlearnpayouts:latest`.

## Данные
`/mnt/user/appdata/kidlearnpayouts/`:
- `uchet.db` — записи и выплаты (бэкап = копия файла);
- `tariffs.json` — тарифы и имя ученика (правятся на странице «Тарифы» после входа по паролю);
- `secret.key` — ключ сессий.

## Безопасность
- После 5 неверных паролей вход блокируется на 5 минут; смена `ADMIN_PASSWORD` разлогинивает все сессии.
- Изменяющие действия защищены CSRF-токеном, выплата дополнительно сверяет актуальную сумму.
- Без HTTPS в интернет не открывайте: reverse proxy (`TRUST_PROXY=1`, `COOKIE_SECURE=1`) или VPN.
