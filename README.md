# TgMultyAI

Локальный OpenAI-похожий HTTP API поверх Telegram-бота Алисы.

Сервер рассчитан на запуск на локальном адресе, например `127.0.0.1:8000`.

## Установка

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Заполните `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` в `.env`. Их можно получить в Telegram API tools.

## Аккаунты

Удобнее хранить аккаунты в `tg_multyai_accounts.json`, а не в длинной строке `.env`.

Если аккаунты и прокси уже заданы через `ALICE_ACCOUNTS` и `ALICE_TELEGRAM_PROXIES`, их можно перенести в JSON:

```powershell
python tg_multyai.py accounts import-env
```

Добавить аккаунт с телефоном:

```powershell
python tg_multyai.py accounts add acc1 --phone +1234567890
```

Добавить аккаунт сразу с прокси:

```powershell
python tg_multyai.py accounts add acc2 `
  --phone +1234567891 `
  --proxy socks5://user:password@proxy.example.com:1080
```

Посмотреть список:

```powershell
python tg_multyai.py accounts list
```

Удалить, выключить или включить аккаунт:

```powershell
python tg_multyai.py accounts remove acc2
python tg_multyai.py accounts disable acc1
python tg_multyai.py accounts enable acc1
```

Изменить телефон или прокси:

```powershell
python tg_multyai.py accounts phone set acc1 +1234567890
python tg_multyai.py accounts proxy set acc1 socks5://user:password@proxy.example.com:1080
python tg_multyai.py accounts proxy remove acc1
```

Пример `tg_multyai_accounts.json`:

```json
{
  "accounts": [
    {
      "name": "acc1",
      "phone": "+1234567890",
      "proxy": "socks5://user:password@proxy.example.com:1080"
    },
    {
      "name": "acc2",
      "phone": "+1234567891"
    }
  ]
}
```

`ALICE_ACCOUNTS` и `--accounts` всё ещё можно использовать как временный фильтр списка аккаунтов. Если они не заданы, сервер берёт все включённые аккаунты из `tg_multyai_accounts.json`.

## Инициализация Сессий

```powershell
python tg_multyai.py init-sessions
```

Если для аккаунта в `tg_multyai_accounts.json` указан `phone`, Telethon использует его автоматически и не будет спрашивать номер вручную. Код Telegram и 2FA-пароль, если нужен, всё равно вводятся интерактивно.

Если нужно создать только часть сессий:

```powershell
python tg_multyai.py init-sessions --accounts acc1,acc2
```

По умолчанию используется `@alice_ya_bot`. При необходимости можно указать другого бота:

```powershell
python tg_multyai.py init-sessions --bot @bot_username
```

## Запуск API

```powershell
python tg_multyai.py serve --host 127.0.0.1 --port 8000
```

Проверка:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

## Chat Completions

```powershell
$body = @{
  model = "alice-telegram"
  messages = @(
    @{ role = "user"; content = "Привет! Объясни рекурсию простыми словами." }
  )
} | ConvertTo-Json -Depth 8

Invoke-RestMethod http://127.0.0.1:8000/v1/chat/completions `
  -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body $body
```

Ответ совместим по форме с `chat.completion`: основной текст находится в `choices[0].message.content`.

## Асинхронная Очередь

```powershell
Invoke-RestMethod http://127.0.0.1:8000/v1/chat/completions/async `
  -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body $body
```

Проверка:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/v1/jobs/<job_id>
```

Запросы распределяются по свободным аккаунтам. На одном Telegram-аккаунте запросы обрабатываются последовательно, чтобы не смешивать контекст бота.

## Файлы

Для HTTP-загрузки файлов используйте multipart endpoint:

```powershell
$payload = '{"messages":[{"role":"user","content":"Что в этом файле?"}]}'
curl.exe -F "payload=$payload" -F "uploads=@C:\path\file.pdf" http://127.0.0.1:8000/v1/chat/completions/multipart
```

Локальные пути к файлам ограничены текущей директорией запуска. Дополнительные разрешённые директории можно задать через `ALICE_ALLOWED_FILE_ROOTS` или `--allowed-file-root`.

## Контекст И Сброс

Получить локально сохранённый контекст:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/v1/context
```

Сбросить контекст на всех аккаунтах:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/v1/context/reset `
  -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body '{"all":true,"force":true}'
```

Важно: реальный контекст Telegram-бота хранится на стороне бота и аккаунта. Локальный `/v1/context` показывает только историю, прошедшую через этот прокси.

## Ограничения Alice

У Alice есть практические ограничения и нестабильности, которые лучше учитывать на стороне клиента.

Стабильно можно рассчитывать примерно на 3000 символов ответа. Для безопасной работы лучше закладывать лимит ответа 2500-2700 символов, хотя технически Alice иногда может выдать 3500-4000 символов.

Если прикреплять несколько изображений сразу, Alice может путаться в содержимом и видеть только первое изображение. Надёжнее отправлять изображения по одному за запрос.
