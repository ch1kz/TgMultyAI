# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Deque, Literal
from urllib.parse import parse_qs, unquote, urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from telethon import TelegramClient, errors, events


DEFAULT_BOT_REF = "8310045254"
RESET_TEXT = "🧹 Новый диалог"

DEFAULT_SERVICE_PATTERNS: tuple[str, ...] = (
    r"^дайте мне немного времени,?\s*уже готовлю ответ[.!?]*$",
    r"^ушла за ответом,?\s*скоро вернусь[.!?]*$",
    r"^обрабатываю запрос,?\s*скоро вернусь с ответом[.!?]*$",
    r"^сейчас я отвечаю на вопрос, который вы задали чуть раньше\..*$",
    r"^обрабатываю\s+(изображение|картинку|файл|документ),?\s*подождите[.!?]*$",
    r"^анализирую\s+(изображение|картинку|файл|документ),?\s*подождите[.!?]*$",
    r"^смотрю\s+(изображение|картинку|файл|документ).*$",
    r"^[^a-zа-я0-9]*(контекст|диалог)(\s+чата)?\s+(успешно\s+)?(сброшен|очищен|обновлен|создан|обновился|очистился).*$",
    r"^[^a-zа-я0-9]*новый диалог\s+(создан|начат|готов).*$",
)

ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DEFAULT_LOCAL_CORS_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
DEFAULT_ACCOUNTS_FILE = "tg_multyai_accounts.json"


class TgMultyAIError(Exception):
    """Base error for expected proxy failures."""


class NoRealAnswerTimeout(TgMultyAIError):
    """The bot did not produce a non-service answer before the timeout."""


class RequestCancelledByReset(TgMultyAIError):
    """An in-flight request was cancelled by a forced context reset."""


def normalize_text(text: str) -> str:
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = text.strip().lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text)


def is_service_message(text: str, patterns: tuple[str, ...]) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False

    reset_normalized = normalize_text(RESET_TEXT)
    if normalized == reset_normalized:
        return True

    return any(re.search(pattern, normalized) for pattern in patterns)


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in re.split(r"[,;\n]+", value) if part.strip())


def parse_csv_many(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        return parse_csv(values)

    items: list[str] = []
    for value in values:
        items.extend(parse_csv(str(value)))
    return tuple(items)


def parse_int(value: str | None, default: int, *, minimum: int = 0) -> int:
    if value is None or str(value).strip() == "":
        return default
    parsed = int(value)
    if parsed < minimum:
        raise ValueError(f"Expected integer >= {minimum}")
    return parsed


def parse_float(value: str | None, default: float, *, minimum: float = 0.0) -> float:
    if value is None or str(value).strip() == "":
        return default
    parsed = float(value)
    if parsed < minimum:
        raise ValueError(f"Expected number >= {minimum}")
    return parsed


def validate_account_name(name: str) -> str:
    if not ACCOUNT_NAME_RE.fullmatch(name):
        raise ValueError(
            "Invalid account name. Use 1-64 characters: letters, digits, dot, underscore, or dash; "
            "the first character must be a letter or digit."
        )
    return name


def parse_accounts(value: str | None) -> list[str]:
    if not value:
        return []
    accounts = [validate_account_name(part.strip()) for part in value.split(",") if part.strip()]
    return accounts


def normalize_phone_number(value: str | None) -> str | None:
    if value is None:
        return None
    phone = re.sub(r"[\s().-]+", "", value.strip())
    if not phone:
        return None
    if not phone.startswith("+") or not phone[1:].isdigit():
        raise ValueError("Phone number must look like +1234567890")
    return phone


def resolve_path(path: str) -> Path:
    expanded = Path(os.path.expandvars(path)).expanduser()
    if not expanded.is_absolute():
        expanded = (Path.cwd() / expanded).resolve()
    return expanded


def parse_file_roots(value: str | None) -> tuple[Path, ...]:
    if not value:
        return (Path.cwd().resolve(),)

    roots = []
    for part in re.split(r"[;\n]+", value):
        item = part.strip()
        if item:
            roots.append(resolve_path(item).resolve())
    return tuple(roots) or (Path.cwd().resolve(),)


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def public_host_is_loopback(host: str) -> bool:
    host = host.strip().strip("[]").lower()
    if host in {"localhost"}:
        return True
    if host in {"0.0.0.0", "::", ""}:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_telegram_proxy(value: str | None, *, setting_name: str = "ALICE_TELEGRAM_PROXY") -> dict[str, Any] | None:
    if not value:
        return None

    parsed = urlparse(value.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"socks5", "socks4", "http"}:
        raise ValueError(f"{setting_name} must use socks5://, socks4://, or http://")
    if not parsed.hostname or parsed.port is None:
        raise ValueError(f"{setting_name} must include host and port")

    query = parse_qs(parsed.query)
    rdns_values = query.get("rdns") or query.get("remote_dns")
    rdns = parse_bool(rdns_values[-1], default=True) if rdns_values else True

    return {
        "proxy_type": scheme,
        "addr": parsed.hostname,
        "port": parsed.port,
        "username": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
        "rdns": rdns,
    }


def parse_telegram_proxy_map(values: Any, *, setting_name: str = "ALICE_TELEGRAM_PROXIES") -> dict[str, dict[str, Any]]:
    if values is None:
        return {}
    if isinstance(values, str):
        raw_items = re.split(r"[;\n]+", values)
    else:
        raw_items = []
        for value in values:
            raw_items.extend(re.split(r"[;\n]+", str(value)))

    proxies: dict[str, dict[str, Any]] = {}
    for raw_item in raw_items:
        item = raw_item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"{setting_name} entries must look like account=socks5://user:pass@host:port")
        account, proxy_url = item.split("=", 1)
        account = validate_account_name(account.strip())
        proxy = parse_telegram_proxy(proxy_url.strip(), setting_name=f"{setting_name} for {account}")
        if proxy is None:
            raise ValueError(f"{setting_name} for {account} is empty")
        proxies[account] = proxy
    return proxies


@dataclass
class AccountRegistryEntry:
    name: str
    phone: str | None = None
    proxy: str | None = None
    disabled: bool = False


def load_account_registry(path: Path) -> OrderedDict[str, AccountRegistryEntry]:
    if not path.exists():
        return OrderedDict()

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid accounts file {path}: {exc}") from exc

    raw_accounts = data.get("accounts", data) if isinstance(data, dict) else data
    if not isinstance(raw_accounts, list):
        raise ValueError(f"Accounts file {path} must contain a list or an object with an 'accounts' list")

    accounts: OrderedDict[str, AccountRegistryEntry] = OrderedDict()
    for index, item in enumerate(raw_accounts, start=1):
        if isinstance(item, str):
            name = validate_account_name(item)
            entry = AccountRegistryEntry(name=name)
        elif isinstance(item, dict):
            name_value = item.get("name") or item.get("session") or item.get("session_name")
            if not name_value:
                raise ValueError(f"Account #{index} in {path} has no name")
            name = validate_account_name(str(name_value).strip())
            phone = normalize_phone_number(str(item["phone"])) if item.get("phone") else None
            proxy = str(item["proxy"]).strip() if item.get("proxy") else None
            if proxy:
                parse_telegram_proxy(proxy, setting_name=f"proxy for account {name}")
            entry = AccountRegistryEntry(
                name=name,
                phone=phone,
                proxy=proxy,
                disabled=parse_bool(str(item.get("disabled")), default=False) if "disabled" in item else False,
            )
        else:
            raise ValueError(f"Account #{index} in {path} must be a string or object")
        accounts[entry.name] = entry

    return accounts


def save_account_registry(path: Path, accounts: OrderedDict[str, AccountRegistryEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "accounts": [
            {
                "name": entry.name,
                **({"phone": entry.phone} if entry.phone else {}),
                **({"proxy": entry.proxy} if entry.proxy else {}),
                **({"disabled": True} if entry.disabled else {}),
            }
            for entry in accounts.values()
        ]
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def mask_phone(phone: str | None) -> str:
    if not phone:
        return "-"
    if len(phone) <= 5:
        return phone[0] + "***"
    return phone[:3] + "*" * max(3, len(phone) - 6) + phone[-3:]


def mask_proxy_url(proxy: str | None) -> str:
    if not proxy:
        return "-"
    parsed = urlparse(proxy)
    if not parsed.hostname:
        return "***"
    auth = "***@" if parsed.username or parsed.password else ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{auth}{parsed.hostname}{port}"


def model_dump_compat(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    accounts: list[str]
    sessions_dir: Path
    accounts_file: Path
    bot_ref: str = DEFAULT_BOT_REF
    request_timeout: float = 120.0
    answer_idle_timeout: float = 4.0
    reset_timeout: float = 15.0
    telegram_message_limit: int = 4096
    telegram_caption_limit: int = 900
    context_limit: int = 200
    service_patterns: tuple[str, ...] = DEFAULT_SERVICE_PATTERNS
    cors: bool = False
    cors_origins: tuple[str, ...] = ()
    cors_origin_regex: str | None = DEFAULT_LOCAL_CORS_ORIGIN_REGEX
    allowed_file_roots: tuple[Path, ...] = field(default_factory=lambda: (Path.cwd().resolve(),))
    allow_any_file_path: bool = False
    telegram_proxy: dict[str, Any] | None = None
    telegram_proxy_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    account_phone_map: dict[str, str] = field(default_factory=dict)


class ChatMessage(BaseModel):
    role: str
    content: Any = ""
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    model: str = "alice-telegram"
    messages: list[ChatMessage] = Field(default_factory=list)
    prompt: str | None = None
    stream: bool = False
    account: str | None = None
    conversation_id: str | None = None
    files: list[str] = Field(default_factory=list)
    timeout: float | None = None
    idle_timeout: float | None = None
    reset_context: bool = False
    send_mode: Literal["full_messages", "last_user"] = "full_messages"


class ContextResetRequest(BaseModel):
    account: str | None = None
    conversation_id: str | None = None
    all: bool = False
    force: bool = True
    wait: bool = True


@dataclass
class PromptBundle:
    prompt: str
    files: list[Path]


@dataclass
class CompletionPayload:
    request_id: str
    conversation_id: str | None
    prompt: str
    files: list[Path]
    timeout: float
    idle_timeout: float
    reset_context: bool
    original_messages: list[dict[str, Any]]


@dataclass
class CompletionResult:
    request_id: str
    account: str
    answer: str
    sources_raw: str | None
    sources: list[str]
    prompt: str
    sent_message_ids: list[int]
    answer_message_ids: list[int]
    created: int
    conversation_id: str | None = None


@dataclass
class ResetPayload:
    wait: bool = True


@dataclass
class ResetResult:
    account: str
    sent_message_id: int | None
    confirmed: bool
    observed_service_messages: list[str]


@dataclass
class InboundMessage:
    message_id: int
    text: str
    edited: bool
    received_monotonic: float


@dataclass
class AccountJob:
    kind: Literal["chat", "reset"]
    payload: CompletionPayload | ResetPayload
    future: asyncio.Future


@dataclass(order=True)
class QueuedJob:
    priority: int
    sequence: int
    job: AccountJob = field(compare=False)


def extract_content(content: Any) -> tuple[str, list[str]]:
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if isinstance(content, (int, float, bool)):
        return str(content), []

    texts: list[str] = []
    files: list[str] = []

    if isinstance(content, list):
        for part in content:
            part_text, part_files = extract_content_part(part)
            if part_text:
                texts.append(part_text)
            files.extend(part_files)
        return "\n".join(texts).strip(), files

    if isinstance(content, dict):
        return extract_content_part(content)

    return str(content), []


def extract_content_part(part: Any) -> tuple[str, list[str]]:
    if isinstance(part, str):
        return part, []
    if not isinstance(part, dict):
        return str(part), []

    part_type = str(part.get("type", "")).lower()
    if part_type in {"text", "input_text"}:
        return str(part.get("text", "")), []

    if part_type in {"file", "input_file"}:
        file_obj = part.get("file", part)
        file_path = extract_file_path(file_obj)
        return "", [file_path] if file_path else []

    if "text" in part:
        return str(part.get("text", "")), []

    file_path = extract_file_path(part)
    return "", [file_path] if file_path else []


def extract_file_path(file_obj: Any) -> str | None:
    if isinstance(file_obj, str):
        return file_obj
    if not isinstance(file_obj, dict):
        return None
    for key in ("path", "file_path", "filename", "name"):
        value = file_obj.get(key)
        if value:
            return str(value)
    if file_obj.get("file_id"):
        raise ValueError("file_id is not supported here; pass a local path or use the multipart endpoint")
    return None


def validate_file_paths(
    files: list[Path],
    settings: Settings,
    extra_allowed_roots: list[Path] | None = None,
) -> list[Path]:
    allowed_roots = tuple(root.resolve() for root in settings.allowed_file_roots)
    if extra_allowed_roots:
        allowed_roots = allowed_roots + tuple(root.resolve() for root in extra_allowed_roots)

    resolved_files: list[Path] = []
    for path in files:
        resolved = path.resolve()
        if not resolved.is_file():
            raise ValueError(f"File not found: {resolved}")
        if not settings.allow_any_file_path and not any(path_is_within(resolved, root) for root in allowed_roots):
            roots = ", ".join(str(root) for root in allowed_roots)
            raise ValueError(f"File is outside allowed roots: {resolved}. Allowed roots: {roots}")
        resolved_files.append(resolved)

    return resolved_files


def positive_timeout(value: float | None, default: float, field_name: str) -> float:
    timeout = default if value is None else float(value)
    if timeout <= 0:
        raise ValueError(f"{field_name} must be positive")
    return timeout


def build_prompt_bundle(
    request: ChatCompletionRequest,
    settings: Settings,
    extra_allowed_file_roots: list[Path] | None = None,
) -> PromptBundle:
    texts: list[str] = []
    file_refs: list[str] = list(request.files)

    if request.prompt:
        texts.append(request.prompt)

    messages = request.messages
    if messages:
        selected_messages = messages
        if request.send_mode == "last_user":
            selected_messages = [messages[-1]]
            for message in reversed(messages):
                if message.role == "user":
                    selected_messages = [message]
                    break

        multi_message = len(selected_messages) > 1
        for message in selected_messages:
            text, message_files = extract_content(message.content)
            file_refs.extend(message_files)
            if not text:
                continue
            if multi_message:
                texts.append(f"{message.role}: {text}")
            else:
                texts.append(text)

    prompt = "\n\n".join(part.strip() for part in texts if part.strip()).strip()

    if not prompt and not file_refs:
        raise ValueError("Request must contain at least one text message, prompt, or file")

    files = validate_file_paths(
        [resolve_path(file_ref) for file_ref in file_refs],
        settings,
        extra_allowed_roots=extra_allowed_file_roots,
    )

    return PromptBundle(prompt=prompt, files=files)


def split_answer_sources(answer: str) -> tuple[str, str | None, list[str]]:
    marker = "━━━━━━━━━━━━━━━━━━"
    if marker in answer:
        before, after = answer.rsplit(marker, 1)
    else:
        match = re.search(r"\n\s*На основе:?", answer, flags=re.IGNORECASE)
        if not match:
            return answer.strip(), None, []
        before = answer[: match.start()]
        after = answer[match.start() :]

    raw_sources = after.strip()
    if not raw_sources or "На основе" not in raw_sources:
        return answer.strip(), None, []

    source_items = parse_source_items(raw_sources)
    return before.strip(), raw_sources, source_items


def parse_source_items(raw_sources: str) -> list[str]:
    text = raw_sources.strip()
    text = re.sub(r"^\s*На основе:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*На основе\s+", "", text, flags=re.IGNORECASE)
    text = text.replace("\r\n", "\n")

    items: list[str] = []
    for line in text.splitlines():
        clean = line.strip()
        if not clean:
            continue
        clean = re.sub(r"^\d+\.\s*", "", clean)
        clean = re.sub(r"^А также:\s*", "", clean, flags=re.IGNORECASE)
        comma_parts = [part.strip(" .;") for part in re.split(r",\s*", clean) if part.strip(" .;")]
        parts: list[str] = []
        for part in comma_parts:
            whitespace_parts = [item.strip(" .;") for item in part.split() if item.strip(" .;")]
            if len(whitespace_parts) > 1 and all("." in item and ":" not in item for item in whitespace_parts):
                parts.extend(whitespace_parts)
            else:
                parts.append(part)
        items.extend(parts)

    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = normalize_text(item)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(item)
    return deduped


class TelegramAccount:
    def __init__(self, name: str, settings: Settings) -> None:
        self.name = name
        self.settings = settings
        self.client: TelegramClient | None = None
        self.bot_entity: Any = None
        self.me: Any = None
        self.inbox: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.queue: asyncio.PriorityQueue[QueuedJob] = asyncio.PriorityQueue()
        self.context: Deque[dict[str, Any]] = deque(maxlen=settings.context_limit)
        self.worker_task: asyncio.Task | None = None
        self.sequence = 0
        self.busy = False
        self.active_cancel: asyncio.Event | None = None
        self.active_request_id: str | None = None

    @property
    def session_path(self) -> Path:
        return (self.settings.sessions_dir / self.name).resolve()

    @property
    def load(self) -> int:
        return self.queue.qsize() + (1 if self.busy else 0)

    @property
    def telegram_proxy(self) -> dict[str, Any] | None:
        return self.settings.telegram_proxy_map.get(self.name) or self.settings.telegram_proxy

    @property
    def phone_number(self) -> str | None:
        return self.settings.account_phone_map.get(self.name)

    async def connect(self, start_bot: bool = False, require_bot: bool = True) -> None:
        self.settings.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(
            str(self.session_path),
            self.settings.api_id,
            self.settings.api_hash,
            proxy=self.telegram_proxy,
        )
        await self.client.start(phone=self.phone_number)
        self.me = await self.client.get_me()
        try:
            self.bot_entity = await self._resolve_bot_entity()
        except RuntimeError:
            if require_bot:
                raise
            self.bot_entity = None
            return

        self.client.add_event_handler(self._handle_new_message, events.NewMessage(chats=self.bot_entity))
        self.client.add_event_handler(self._handle_edited_message, events.MessageEdited(chats=self.bot_entity))

        if start_bot:
            with contextlib.suppress(Exception):
                await self._safe_send_message("/start")
                await asyncio.sleep(1.0)
                self._drain_inbox()

        self.worker_task = asyncio.create_task(self._worker_loop(), name=f"tg-multyai-{self.name}")

    async def disconnect(self) -> None:
        if self.worker_task:
            self.worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker_task
        if self.client:
            await self.client.disconnect()

    async def _resolve_bot_entity(self) -> Any:
        assert self.client is not None
        ref = self.settings.bot_ref.strip()
        entity_ref: str | int = int(ref) if ref.isdigit() else ref
        try:
            return await self.client.get_entity(entity_ref)
        except Exception as exc:
            raise RuntimeError(
                f"Account '{self.name}' cannot resolve bot '{self.settings.bot_ref}'. "
                "If this is a fresh session, start the bot manually once or pass its @username via --bot/ALICE_BOT."
            ) from exc

    async def _handle_new_message(self, event: Any) -> None:
        await self._push_inbound(event, edited=False)

    async def _handle_edited_message(self, event: Any) -> None:
        await self._push_inbound(event, edited=True)

    async def _push_inbound(self, event: Any, edited: bool) -> None:
        if getattr(event, "out", False) or getattr(event.message, "out", False):
            return
        message = event.message
        await self.inbox.put(
            InboundMessage(
                message_id=message.id,
                text=message.raw_text or "",
                edited=edited,
                received_monotonic=time.monotonic(),
            )
        )

    async def submit_chat(self, payload: CompletionPayload) -> CompletionResult:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        await self._put_job(priority=10, job=AccountJob(kind="chat", payload=payload, future=future))
        return await future

    async def submit_reset(self, force: bool = True, wait: bool = True) -> ResetResult:
        if force and self.active_cancel:
            self.active_cancel.set()
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        await self._put_job(priority=0 if force else 5, job=AccountJob(kind="reset", payload=ResetPayload(wait=wait), future=future))
        return await future

    async def _put_job(self, priority: int, job: AccountJob) -> None:
        self.sequence += 1
        await self.queue.put(QueuedJob(priority=priority, sequence=self.sequence, job=job))

    async def _worker_loop(self) -> None:
        while True:
            queued = await self.queue.get()
            job = queued.job
            self.busy = True
            self.active_request_id = None
            self.active_cancel = None

            try:
                if job.kind == "chat":
                    payload = job.payload
                    assert isinstance(payload, CompletionPayload)
                    cancel_event = asyncio.Event()
                    self.active_cancel = cancel_event
                    self.active_request_id = payload.request_id
                    result = await self._process_chat(payload, cancel_event)
                    if not job.future.done():
                        job.future.set_result(result)
                else:
                    payload = job.payload
                    assert isinstance(payload, ResetPayload)
                    result = await self._process_reset(payload)
                    if not job.future.done():
                        job.future.set_result(result)
            except RequestCancelledByReset as exc:
                if not job.future.done():
                    job.future.set_exception(exc)
            except Exception as exc:
                if not job.future.done():
                    job.future.set_exception(exc)
            finally:
                self.active_cancel = None
                self.active_request_id = None
                self.busy = False
                self.queue.task_done()

    async def _process_chat(self, payload: CompletionPayload, cancel_event: asyncio.Event) -> CompletionResult:
        if payload.reset_context:
            await self._send_reset(wait=True, cancel_event=cancel_event)
            self.context.clear()

        self._drain_inbox()
        sent_ids = await self._send_request_payload(payload, cancel_event)
        if cancel_event.is_set():
            raise RequestCancelledByReset("Request was cancelled before waiting for an answer")

        answer, answer_ids = await self._wait_for_answer(
            sent_after_id=max(sent_ids),
            timeout=payload.timeout,
            idle_timeout=payload.idle_timeout,
            cancel_event=cancel_event,
        )
        clean_answer, sources_raw, sources = split_answer_sources(answer)

        self.context.append(
            {
                "role": "user",
                "content": payload.prompt,
                "files": [str(path) for path in payload.files],
                "created": int(time.time()),
                "request_id": payload.request_id,
                "conversation_id": payload.conversation_id,
                "telegram_message_ids": sent_ids,
            }
        )
        self.context.append(
            {
                "role": "assistant",
                "content": clean_answer,
                "sources": sources,
                "sources_raw": sources_raw,
                "created": int(time.time()),
                "request_id": payload.request_id,
                "conversation_id": payload.conversation_id,
                "telegram_message_ids": answer_ids,
            }
        )

        return CompletionResult(
            request_id=payload.request_id,
            account=self.name,
            answer=clean_answer,
            sources_raw=sources_raw,
            sources=sources,
            prompt=payload.prompt,
            sent_message_ids=sent_ids,
            answer_message_ids=answer_ids,
            created=int(time.time()),
            conversation_id=payload.conversation_id,
        )

    async def _process_reset(self, payload: ResetPayload) -> ResetResult:
        result = await self._send_reset(wait=payload.wait, cancel_event=None)
        self.context.clear()
        return result

    async def _send_request_payload(self, payload: CompletionPayload, cancel_event: asyncio.Event) -> list[int]:
        sent_ids: list[int] = []
        prompt_as_text = payload.prompt.strip()
        prompt_fits_message = len(prompt_as_text) <= self.settings.telegram_message_limit

        if payload.files:
            files_to_send = list(payload.files)
            temp_prompt_dir: tempfile.TemporaryDirectory[str] | None = None
            caption = prompt_as_text if prompt_as_text and len(prompt_as_text) <= self.settings.telegram_caption_limit else None
            if prompt_as_text and caption is None:
                temp_prompt_dir, prompt_file = self._make_prompt_file(prompt_as_text)
                files_to_send.append(prompt_file)
                caption = "Запрос во вложенном TXT-файле."

            try:
                result = await self._safe_send_file(files_to_send, caption=caption)
            finally:
                if temp_prompt_dir:
                    temp_prompt_dir.cleanup()
            sent_ids.extend(self._extract_sent_ids(result))

            if cancel_event.is_set():
                raise RequestCancelledByReset("Request was cancelled while sending files")
        elif prompt_as_text and not prompt_fits_message:
            temp_prompt_dir, prompt_file = self._make_prompt_file(prompt_as_text)
            try:
                result = await self._safe_send_file([prompt_file], caption="Запрос во вложенном TXT-файле.")
            finally:
                temp_prompt_dir.cleanup()
            sent_ids.extend(self._extract_sent_ids(result))
        else:
            message = await self._safe_send_message(prompt_as_text)
            sent_ids.append(message.id)

        return sent_ids

    def _make_prompt_file(self, prompt: str) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temp_dir = tempfile.TemporaryDirectory(prefix="tg-multyai-prompt-")
        path = Path(temp_dir.name) / f"prompt-{uuid.uuid4().hex}.txt"
        path.write_text(prompt, encoding="utf-8")
        return temp_dir, path

    async def _wait_for_answer(
        self,
        sent_after_id: int,
        timeout: float,
        idle_timeout: float,
        cancel_event: asyncio.Event,
    ) -> tuple[str, list[int]]:
        deadline = time.monotonic() + timeout
        candidates: OrderedDict[int, str] = OrderedDict()
        last_candidate_at: float | None = None

        while time.monotonic() < deadline:
            if cancel_event.is_set():
                raise RequestCancelledByReset("Request was cancelled by context reset")

            now = time.monotonic()
            if candidates and last_candidate_at is not None:
                idle_left = idle_timeout - (now - last_candidate_at)
                if idle_left <= 0:
                    break
                wait_for = min(0.5, idle_left, deadline - now)
            else:
                wait_for = min(0.5, deadline - now)

            if wait_for <= 0:
                break

            try:
                inbound = await asyncio.wait_for(self.inbox.get(), timeout=wait_for)
            except asyncio.TimeoutError:
                continue

            if inbound.message_id <= sent_after_id:
                continue

            text = inbound.text.strip()
            if not text:
                continue

            if is_service_message(text, self.settings.service_patterns):
                continue

            candidates[inbound.message_id] = text
            last_candidate_at = time.monotonic()

        if not candidates:
            raise NoRealAnswerTimeout("No non-service answer was received from the bot")

        parts: list[str] = []
        seen: set[str] = set()
        for text in candidates.values():
            normalized = normalize_text(text)
            if normalized in seen:
                continue
            seen.add(normalized)
            parts.append(text)

        return "\n\n".join(parts), list(candidates.keys())

    async def _send_reset(self, wait: bool, cancel_event: asyncio.Event | None) -> ResetResult:
        self._drain_inbox()
        sent = await self._safe_send_message(RESET_TEXT)
        if not wait:
            return ResetResult(account=self.name, sent_message_id=sent.id, confirmed=False, observed_service_messages=[])

        deadline = time.monotonic() + self.settings.reset_timeout
        observed: list[str] = []
        confirmed = False
        last_event_at: float | None = None

        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                raise RequestCancelledByReset("Request was cancelled during reset")

            wait_for = min(0.5, deadline - time.monotonic())
            if wait_for <= 0:
                break

            try:
                inbound = await asyncio.wait_for(self.inbox.get(), timeout=wait_for)
            except asyncio.TimeoutError:
                if confirmed and last_event_at and time.monotonic() - last_event_at >= 1.0:
                    break
                continue

            if inbound.message_id <= sent.id:
                continue

            text = inbound.text.strip()
            if not text:
                continue

            observed.append(text)
            last_event_at = time.monotonic()
            if is_service_message(text, self.settings.service_patterns):
                confirmed = True
                last_event_at = time.monotonic()

        return ResetResult(
            account=self.name,
            sent_message_id=sent.id,
            confirmed=confirmed,
            observed_service_messages=observed,
        )

    async def _safe_send_message(self, text: str) -> Any:
        assert self.client is not None
        for attempt in range(2):
            try:
                return await self.client.send_message(self.bot_entity, text)
            except errors.FloodWaitError as exc:
                if attempt == 1:
                    raise
                await asyncio.sleep(exc.seconds + 1)

    async def _safe_send_file(self, files: list[Path], caption: str | None) -> Any:
        assert self.client is not None
        for attempt in range(2):
            try:
                return await self.client.send_file(
                    self.bot_entity,
                    [str(path) for path in files],
                    caption=caption,
                )
            except errors.FloodWaitError as exc:
                if attempt == 1:
                    raise
                await asyncio.sleep(exc.seconds + 1)

    @staticmethod
    def _extract_sent_ids(result: Any) -> list[int]:
        if isinstance(result, list):
            return [item.id for item in result if hasattr(item, "id")]
        if hasattr(result, "id"):
            return [result.id]
        return []

    def _drain_inbox(self) -> None:
        while True:
            try:
                self.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break

    def snapshot(self) -> dict[str, Any]:
        user = None
        if self.me:
            user = {
                "id": getattr(self.me, "id", None),
                "username": getattr(self.me, "username", None),
                "first_name": getattr(self.me, "first_name", None),
            }
        return {
            "name": self.name,
            "user": user,
            "busy": self.busy,
            "active_request_id": self.active_request_id,
            "queue_size": self.queue.qsize(),
            "context_size": len(self.context),
            "proxy": self._proxy_snapshot(),
        }

    def context_snapshot(self) -> list[dict[str, Any]]:
        return list(self.context)

    def _proxy_snapshot(self) -> dict[str, Any] | None:
        proxy = self.telegram_proxy
        if not proxy:
            return None
        return {
            "proxy_type": proxy.get("proxy_type"),
            "addr": proxy.get("addr"),
            "port": proxy.get("port"),
            "username": bool(proxy.get("username")),
            "rdns": proxy.get("rdns"),
        }


class TgMultyAIPool:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.accounts = {name: TelegramAccount(name, settings) for name in settings.accounts}
        self.conversation_accounts: dict[str, str] = {}

    async def start(self, start_bot: bool = False, require_bot: bool = True) -> None:
        for account in self.accounts.values():
            await account.connect(start_bot=start_bot, require_bot=require_bot)

    async def stop(self) -> None:
        await asyncio.gather(*(account.disconnect() for account in self.accounts.values()), return_exceptions=True)

    def select_account(self, account_name: str | None = None, conversation_id: str | None = None) -> TelegramAccount:
        if account_name:
            if account_name not in self.accounts:
                raise ValueError(f"Unknown account: {account_name}")
            return self.accounts[account_name]

        if conversation_id and conversation_id in self.conversation_accounts:
            return self.accounts[self.conversation_accounts[conversation_id]]

        account = min(self.accounts.values(), key=lambda item: (item.load, item.name))
        if conversation_id:
            self.conversation_accounts[conversation_id] = account.name
        return account

    async def chat(
        self,
        request: ChatCompletionRequest,
        extra_allowed_file_roots: list[Path] | None = None,
    ) -> CompletionResult:
        bundle = build_prompt_bundle(request, self.settings, extra_allowed_file_roots=extra_allowed_file_roots)
        account = self.select_account(request.account, request.conversation_id)
        payload = CompletionPayload(
            request_id="chatcmpl-" + uuid.uuid4().hex,
            conversation_id=request.conversation_id,
            prompt=bundle.prompt,
            files=bundle.files,
            timeout=positive_timeout(request.timeout, self.settings.request_timeout, "timeout"),
            idle_timeout=positive_timeout(request.idle_timeout, self.settings.answer_idle_timeout, "idle_timeout"),
            reset_context=request.reset_context,
            original_messages=[model_dump_compat(message) for message in request.messages],
        )
        return await account.submit_chat(payload)

    async def reset(
        self,
        account_name: str | None = None,
        conversation_id: str | None = None,
        all_accounts: bool = False,
        force: bool = True,
        wait: bool = True,
    ) -> list[ResetResult]:
        if all_accounts or (account_name is None and conversation_id is None):
            targets = list(self.accounts.values())
        elif conversation_id:
            if conversation_id not in self.conversation_accounts:
                raise ValueError(f"Unknown conversation_id: {conversation_id}")
            targets = [self.accounts[self.conversation_accounts[conversation_id]]]
            self.conversation_accounts.pop(conversation_id, None)
        else:
            targets = [self.select_account(account_name)]

        return await asyncio.gather(*(account.submit_reset(force=force, wait=wait) for account in targets))

    def context(self, account_name: str | None = None, conversation_id: str | None = None) -> dict[str, Any]:
        if account_name:
            return {account_name: self.select_account(account_name).context_snapshot()}

        if conversation_id:
            account_name = self.conversation_accounts.get(conversation_id)
            if not account_name:
                raise ValueError(f"Unknown conversation_id: {conversation_id}")
            entries = [
                entry
                for entry in self.accounts[account_name].context_snapshot()
                if entry.get("conversation_id") == conversation_id
            ]
            return {conversation_id: entries}

        return {name: account.context_snapshot() for name, account in self.accounts.items()}

    def accounts_snapshot(self) -> list[dict[str, Any]]:
        return [account.snapshot() for account in self.accounts.values()]


def make_completion_response(request: ChatCompletionRequest, result: CompletionResult) -> dict[str, Any]:
    return {
        "id": result.request_id,
        "object": "chat.completion",
        "created": result.created,
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": result.answer,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_chars": len(result.prompt),
            "completion_chars": len(result.answer),
        },
        "sources": {
            "raw": result.sources_raw,
            "items": result.sources,
        },
        "telegram": {
            "account": result.account,
            "conversation_id": result.conversation_id,
            "sent_message_ids": result.sent_message_ids,
            "answer_message_ids": result.answer_message_ids,
        },
    }


async def save_uploads(uploads: list[UploadFile] | None, settings: Settings) -> tuple[Path | None, list[str]]:
    if not uploads:
        return None, []

    temp_dir = Path(tempfile.mkdtemp(prefix="tg-multyai-uploads-"))
    saved: list[str] = []
    try:
        for upload in uploads:
            original_name = Path(upload.filename or "upload.bin").name
            safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", original_name).strip() or "upload.bin"
            safe_name = safe_name[:120]
            destination = temp_dir / f"{uuid.uuid4().hex}_{safe_name}"
            with destination.open("wb") as file:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    file.write(chunk)
            saved.append(str(destination))
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return temp_dir, saved


def http_error_from_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, RequestCancelledByReset):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, NoRealAnswerTimeout):
        return HTTPException(status_code=504, detail=str(exc))
    return HTTPException(status_code=500, detail="Internal server error")


def add_security_headers(response: Any) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")


def build_app(settings: Settings) -> FastAPI:
    jobs: dict[str, dict[str, Any]] = {}

    async def complete_request(
        app: FastAPI,
        chat_request: ChatCompletionRequest,
        extra_allowed_file_roots: list[Path] | None = None,
    ) -> dict[str, Any]:
        if chat_request.stream:
            raise HTTPException(status_code=400, detail="Streaming is not implemented for this Telegram backend")
        try:
            result = await app.state.pool.chat(chat_request, extra_allowed_file_roots=extra_allowed_file_roots)
        except Exception as exc:
            raise http_error_from_exception(exc) from exc
        return make_completion_response(chat_request, result)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = TgMultyAIPool(settings)
        await pool.start(start_bot=False)
        app.state.pool = pool
        try:
            yield
        finally:
            await pool.stop()

    app = FastAPI(title="TgMultyAI", version="1.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def security_middleware(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        add_security_headers(response)
        return response

    if settings.cors:
        allow_origins = list(settings.cors_origins)
        allow_origin_regex = settings.cors_origin_regex
        if "*" in allow_origins:
            allow_origins = ["*"]
            allow_origin_regex = None
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allow_origins,
            allow_origin_regex=allow_origin_regex,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["Content-Type"],
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": "alice-telegram",
                    "object": "model",
                    "created": 0,
                    "owned_by": "telegram",
                }
            ],
        }

    @app.get("/v1/accounts")
    async def accounts() -> dict[str, Any]:
        return {"accounts": app.state.pool.accounts_snapshot()}

    @app.get("/v1/context")
    async def get_context(account: str | None = None, conversation_id: str | None = None) -> dict[str, Any]:
        try:
            return {"context": app.state.pool.context(account_name=account, conversation_id=conversation_id)}
        except Exception as exc:
            raise http_error_from_exception(exc) from exc

    @app.post("/v1/context/reset")
    async def reset_context(request: ContextResetRequest) -> dict[str, Any]:
        try:
            results = await app.state.pool.reset(
                account_name=request.account,
                conversation_id=request.conversation_id,
                all_accounts=request.all,
                force=request.force,
                wait=request.wait,
            )
        except Exception as exc:
            raise http_error_from_exception(exc) from exc

        return {
            "object": "context.reset",
            "results": [
                {
                    "account": result.account,
                    "sent_message_id": result.sent_message_id,
                    "confirmed": result.confirmed,
                    "observed_service_messages": result.observed_service_messages,
                }
                for result in results
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: ChatCompletionRequest) -> dict[str, Any]:
        return await complete_request(app, request)

    @app.post("/v1/chat/completions/async")
    async def chat_completions_async(request: ChatCompletionRequest) -> dict[str, Any]:
        job_id = "job-" + uuid.uuid4().hex
        jobs[job_id] = {
            "id": job_id,
            "object": "chat.completion.job",
            "status": "queued",
            "created": int(time.time()),
            "updated": int(time.time()),
            "result": None,
            "error": None,
        }

        async def run_job() -> None:
            jobs[job_id]["status"] = "running"
            jobs[job_id]["updated"] = int(time.time())
            try:
                jobs[job_id]["result"] = await complete_request(app, request)
                jobs[job_id]["status"] = "succeeded"
            except HTTPException as exc:
                jobs[job_id]["error"] = {"status_code": exc.status_code, "detail": exc.detail}
                jobs[job_id]["status"] = "failed"
            except Exception:
                jobs[job_id]["error"] = {"status_code": 500, "detail": "Internal server error"}
                jobs[job_id]["status"] = "failed"
            finally:
                jobs[job_id]["updated"] = int(time.time())

        asyncio.create_task(run_job())
        return jobs[job_id]

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        if job_id not in jobs:
            raise HTTPException(status_code=404, detail="Unknown job_id")
        return jobs[job_id]

    @app.post("/v1/chat/completions/multipart")
    async def chat_completions_multipart(
        payload: str = Form(...),
        uploads: list[UploadFile] | None = File(default=None),
    ) -> dict[str, Any]:
        temp_dir: Path | None = None
        try:
            data = json.loads(payload.lstrip("\ufeff"))
            request = ChatCompletionRequest.model_validate(data)
            temp_dir, uploaded_paths = await save_uploads(uploads, settings)
            request.files.extend(uploaded_paths)
            return await complete_request(app, request, extra_allowed_file_roots=[temp_dir] if temp_dir else None)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc}") from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise http_error_from_exception(exc) from exc
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    return app


def validate_serve_security(settings: Settings, host: str) -> None:
    # API-key/public-host protection was intentionally removed.
    return


def validate_proxy_accounts(settings: Settings) -> None:
    unknown = sorted(set(settings.telegram_proxy_map) - set(settings.accounts))
    if unknown:
        raise SystemExit(
            "Proxy map contains unknown account(s): "
            + ", ".join(unknown)
            + f". Add them to {settings.accounts_file}, ALICE_ACCOUNTS, or --accounts."
        )


def settings_from_args(args: argparse.Namespace) -> Settings:
    api_id = args.api_id or os.getenv("TELEGRAM_API_ID") or os.getenv("API_ID")
    api_hash = args.api_hash or os.getenv("TELEGRAM_API_HASH") or os.getenv("API_HASH")

    if not api_id or not api_hash:
        raise SystemExit("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env or pass --api-id/--api-hash")

    bot_ref = args.bot or os.getenv("ALICE_BOT") or os.getenv("ALICE_BOT_USERNAME") or os.getenv("ALICE_BOT_ID") or DEFAULT_BOT_REF
    extra_patterns = os.getenv("ALICE_SERVICE_PATTERNS", "")
    patterns = list(DEFAULT_SERVICE_PATTERNS)
    patterns.extend(pattern.strip() for pattern in extra_patterns.split("|") if pattern.strip())
    request_timeout_value = getattr(args, "timeout", None)
    if request_timeout_value is None:
        request_timeout_value = os.getenv("ALICE_REQUEST_TIMEOUT")
    request_timeout = parse_float(request_timeout_value, 120.0, minimum=0.1)

    idle_timeout_value = getattr(args, "idle_timeout", None)
    if idle_timeout_value is None:
        idle_timeout_value = os.getenv("ALICE_ANSWER_IDLE_TIMEOUT")
    answer_idle_timeout = parse_float(idle_timeout_value, 4.0, minimum=0.1)

    reset_timeout_value = getattr(args, "reset_timeout", None)
    if reset_timeout_value is None:
        reset_timeout_value = os.getenv("ALICE_RESET_TIMEOUT")
    reset_timeout = parse_float(reset_timeout_value, 15.0, minimum=0.1)

    cors_origins = parse_csv_many(getattr(args, "cors_origin", None))
    if not cors_origins:
        cors_origins = parse_csv(os.getenv("ALICE_CORS_ORIGINS"))

    cors_regex_value = getattr(args, "cors_origin_regex", None)
    if cors_regex_value is None:
        cors_regex_value = os.getenv("ALICE_CORS_ORIGIN_REGEX")
    cors_origin_regex = DEFAULT_LOCAL_CORS_ORIGIN_REGEX if cors_regex_value is None else (cors_regex_value.strip() or None)
    accounts_file = resolve_path(getattr(args, "accounts_file", None) or os.getenv("ALICE_ACCOUNTS_FILE") or DEFAULT_ACCOUNTS_FILE)
    try:
        account_registry = load_account_registry(accounts_file)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    explicit_accounts = parse_accounts(getattr(args, "accounts", None) or os.getenv("ALICE_ACCOUNTS"))
    if explicit_accounts:
        accounts = explicit_accounts
    else:
        accounts = [name for name, entry in account_registry.items() if not entry.disabled] or ["main"]

    account_phone_map = {
        name: entry.phone
        for name, entry in account_registry.items()
        if entry.phone and name in accounts
    }

    telegram_proxy_value = getattr(args, "telegram_proxy", None) or os.getenv("ALICE_TELEGRAM_PROXY")
    telegram_proxy_map = {
        name: parse_telegram_proxy(entry.proxy, setting_name=f"proxy for account {name}")
        for name, entry in account_registry.items()
        if entry.proxy and name in accounts
    }
    telegram_proxy_map = {name: proxy for name, proxy in telegram_proxy_map.items() if proxy is not None}
    env_proxy_map = parse_telegram_proxy_map(os.getenv("ALICE_TELEGRAM_PROXIES"))
    telegram_proxy_map.update(env_proxy_map)
    cli_proxy_map = parse_telegram_proxy_map(getattr(args, "telegram_proxy_map", None), setting_name="--telegram-proxy-map")
    telegram_proxy_map.update(cli_proxy_map)
    allowed_file_root_args = getattr(args, "allowed_file_root", None)
    if allowed_file_root_args:
        allowed_file_roots = tuple(resolve_path(path).resolve() for path in allowed_file_root_args)
    else:
        allowed_file_roots = parse_file_roots(os.getenv("ALICE_ALLOWED_FILE_ROOTS"))

    return Settings(
        api_id=int(api_id),
        api_hash=str(api_hash),
        accounts=accounts,
        sessions_dir=resolve_path(args.sessions_dir or os.getenv("ALICE_SESSIONS_DIR") or "sessions"),
        accounts_file=accounts_file,
        bot_ref=str(bot_ref),
        request_timeout=request_timeout,
        answer_idle_timeout=answer_idle_timeout,
        reset_timeout=reset_timeout,
        telegram_message_limit=parse_int(os.getenv("ALICE_TELEGRAM_MESSAGE_LIMIT"), 4096, minimum=1),
        telegram_caption_limit=parse_int(os.getenv("ALICE_TELEGRAM_CAPTION_LIMIT"), 900, minimum=1),
        context_limit=parse_int(os.getenv("ALICE_CONTEXT_LIMIT"), 200, minimum=0),
        service_patterns=tuple(patterns),
        cors=parse_bool(os.getenv("ALICE_CORS"), default=False) or bool(getattr(args, "cors", False)) or bool(cors_origins),
        cors_origins=cors_origins,
        cors_origin_regex=cors_origin_regex,
        allowed_file_roots=allowed_file_roots,
        allow_any_file_path=parse_bool(os.getenv("ALICE_ALLOW_ANY_FILE_PATH"), default=False) or bool(getattr(args, "allow_any_file_path", False)),
        telegram_proxy=parse_telegram_proxy(telegram_proxy_value),
        telegram_proxy_map=telegram_proxy_map,
        account_phone_map=account_phone_map,
    )


async def init_sessions(settings: Settings, start_bot: bool) -> None:
    pool = TgMultyAIPool(settings)
    try:
        await pool.start(start_bot=start_bot, require_bot=False)
        for account in pool.accounts.values():
            user = account.snapshot().get("user") or {}
            bot_status = "bot resolved" if account.bot_entity is not None else "bot not resolved yet"
            print(f"Session '{account.name}' is ready: @{user.get('username') or user.get('id')} ({bot_status})")
    finally:
        await pool.stop()


async def ask_once(settings: Settings, args: argparse.Namespace) -> None:
    if args.account:
        settings = replace(settings, accounts=[args.account])

    pool = TgMultyAIPool(settings)
    try:
        await pool.start(start_bot=False)
        request = ChatCompletionRequest(
            messages=[ChatMessage(role="user", content=args.text)],
            files=args.file or [],
            account=args.account,
            timeout=args.timeout,
            idle_timeout=args.idle_timeout,
            reset_context=args.reset_context,
            send_mode=args.send_mode,
        )
        result = await pool.chat(request)
        print(result.answer)
    finally:
        await pool.stop()


def accounts_file_from_args(args: argparse.Namespace) -> Path:
    return resolve_path(getattr(args, "accounts_file", None) or os.getenv("ALICE_ACCOUNTS_FILE") or DEFAULT_ACCOUNTS_FILE)


def load_registry_for_command(args: argparse.Namespace) -> tuple[Path, OrderedDict[str, AccountRegistryEntry]]:
    path = accounts_file_from_args(args)
    try:
        return path, load_account_registry(path)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def print_account_registry(accounts: OrderedDict[str, AccountRegistryEntry], *, show_secrets: bool = False) -> None:
    if not accounts:
        print("No accounts in registry.")
        return

    rows = []
    for entry in accounts.values():
        rows.append(
            (
                entry.name,
                "disabled" if entry.disabled else "enabled",
                entry.phone if show_secrets else mask_phone(entry.phone),
                entry.proxy if show_secrets else mask_proxy_url(entry.proxy),
            )
        )

    widths = [
        max(len("name"), *(len(row[0]) for row in rows)),
        max(len("status"), *(len(row[1]) for row in rows)),
        max(len("phone"), *(len(row[2]) for row in rows)),
        max(len("proxy"), *(len(row[3]) for row in rows)),
    ]
    print(f"{'name':<{widths[0]}}  {'status':<{widths[1]}}  {'phone':<{widths[2]}}  {'proxy':<{widths[3]}}")
    print(f"{'-' * widths[0]}  {'-' * widths[1]}  {'-' * widths[2]}  {'-' * widths[3]}")
    for row in rows:
        print(f"{row[0]:<{widths[0]}}  {row[1]:<{widths[1]}}  {row[2]:<{widths[2]}}  {row[3]:<{widths[3]}}")


def run_accounts_command(args: argparse.Namespace) -> None:
    path, accounts = load_registry_for_command(args)
    command = args.accounts_command

    if command == "list":
        print_account_registry(accounts, show_secrets=args.show_secrets)
        return

    if command == "import-env":
        env_accounts = parse_accounts(os.getenv("ALICE_ACCOUNTS"))
        env_proxies = parse_telegram_proxy_map(os.getenv("ALICE_TELEGRAM_PROXIES"))
        if not env_accounts and not env_proxies:
            raise SystemExit("No ALICE_ACCOUNTS or ALICE_TELEGRAM_PROXIES found in environment.")
        if args.replace:
            accounts = OrderedDict()
        for name in env_accounts:
            accounts.setdefault(name, AccountRegistryEntry(name=name))
        for name in env_proxies:
            accounts.setdefault(name, AccountRegistryEntry(name=name))
        raw_proxy_map = {}
        for raw_item in re.split(r"[;\n]+", os.getenv("ALICE_TELEGRAM_PROXIES") or ""):
            item = raw_item.strip()
            if not item or "=" not in item:
                continue
            name, proxy_url = item.split("=", 1)
            raw_proxy_map[validate_account_name(name.strip())] = proxy_url.strip()
        for name, proxy_url in raw_proxy_map.items():
            accounts[name].proxy = proxy_url
        save_account_registry(path, accounts)
        print(f"Imported {len(accounts)} account(s) into {path}")
        return

    if command == "add":
        name = validate_account_name(args.name)
        if name in accounts and not args.replace:
            raise SystemExit(f"Account '{name}' already exists. Use --replace to update it.")
        phone = normalize_phone_number(args.phone)
        if args.proxy:
            parse_telegram_proxy(args.proxy, setting_name=f"proxy for account {name}")
        existing = accounts.get(name)
        accounts[name] = AccountRegistryEntry(
            name=name,
            phone=phone if phone is not None else (existing.phone if existing else None),
            proxy=args.proxy if args.proxy is not None else (existing.proxy if existing else None),
            disabled=args.disabled if existing is None else (args.disabled or existing.disabled),
        )
        save_account_registry(path, accounts)
        print(f"Account '{name}' saved in {path}")
        return

    if command == "remove":
        name = validate_account_name(args.name)
        if name not in accounts:
            raise SystemExit(f"Account '{name}' is not in {path}")
        accounts.pop(name)
        save_account_registry(path, accounts)
        print(f"Account '{name}' removed from {path}")
        return

    if command in {"enable", "disable"}:
        name = validate_account_name(args.name)
        if name not in accounts:
            raise SystemExit(f"Account '{name}' is not in {path}")
        accounts[name].disabled = command == "disable"
        save_account_registry(path, accounts)
        print(f"Account '{name}' {command}d")
        return

    if command == "phone":
        name = validate_account_name(args.name)
        if name not in accounts:
            raise SystemExit(f"Account '{name}' is not in {path}")
        if args.phone_action == "set":
            accounts[name].phone = normalize_phone_number(args.phone)
            message = f"Phone saved for '{name}'"
        else:
            accounts[name].phone = None
            message = f"Phone removed for '{name}'"
        save_account_registry(path, accounts)
        print(message)
        return

    if command == "proxy":
        name = validate_account_name(args.name)
        if name not in accounts:
            raise SystemExit(f"Account '{name}' is not in {path}")
        if args.proxy_action == "set":
            parse_telegram_proxy(args.proxy, setting_name=f"proxy for account {name}")
            accounts[name].proxy = args.proxy
            message = f"Proxy saved for '{name}'"
        else:
            accounts[name].proxy = None
            message = f"Proxy removed for '{name}'"
        save_account_registry(path, accounts)
        print(message)
        return

    raise SystemExit(f"Unknown accounts command: {command}")


def add_common_cli_args(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument("--api-id", type=int, default=default)
    parser.add_argument("--api-hash", default=default)
    parser.add_argument("--accounts", default=default, help="Comma-separated account/session names")
    parser.add_argument("--accounts-file", default=default, help=f"JSON account registry. Default: {DEFAULT_ACCOUNTS_FILE}")
    parser.add_argument("--sessions-dir", default=default)
    parser.add_argument("--bot", default=default, help="Bot ID or @username. Default: 8310045254")
    parser.add_argument("--timeout", type=float, default=default)
    parser.add_argument("--idle-timeout", type=float, default=default)
    parser.add_argument("--reset-timeout", type=float, default=default)
    parser.add_argument(
        "--telegram-proxy",
        default=default,
        help="Fallback Telegram proxy URL, for example socks5://user:pass@host:1080",
    )
    parser.add_argument(
        "--telegram-proxy-map",
        action="append",
        default=argparse.SUPPRESS if suppress_defaults else None,
        help="Per-account proxy mapping account=socks5://user:pass@host:1080; can be repeated",
    )


def add_serve_security_cli_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cors-origin", action="append", default=argparse.SUPPRESS, help="Allowed CORS origin; can be repeated")
    parser.add_argument("--cors-origin-regex", default=argparse.SUPPRESS, help="Allowed CORS origin regex")
    parser.add_argument(
        "--allowed-file-root",
        action="append",
        default=argparse.SUPPRESS,
        help="Directory from which local file paths may be attached; can be repeated",
    )
    parser.add_argument(
        "--allow-any-file-path",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Disable local file path root restrictions",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenAI-like local API for TgMultyAI")
    add_common_cli_args(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-sessions", help="Create or reuse Telegram sessions")
    add_common_cli_args(init_parser, suppress_defaults=True)
    init_parser.add_argument("--no-start-bot", action="store_true", help="Do not send /start after login")

    accounts_parser = subparsers.add_parser("accounts", help="Manage account registry")
    accounts_parser.add_argument("--accounts-file", default=argparse.SUPPRESS, help=f"JSON account registry. Default: {DEFAULT_ACCOUNTS_FILE}")
    accounts_subparsers = accounts_parser.add_subparsers(dest="accounts_command", required=True)

    accounts_list_parser = accounts_subparsers.add_parser("list", help="List configured accounts")
    accounts_list_parser.add_argument("--show-secrets", action="store_true", help="Show full phone numbers and proxy URLs")

    accounts_import_parser = accounts_subparsers.add_parser("import-env", help="Import ALICE_ACCOUNTS and ALICE_TELEGRAM_PROXIES into the registry")
    accounts_import_parser.add_argument("--replace", action="store_true", help="Replace the current registry instead of merging")

    accounts_add_parser = accounts_subparsers.add_parser("add", help="Add or update an account")
    accounts_add_parser.add_argument("name")
    accounts_add_parser.add_argument("--phone", default=None, help="Phone number, for example +1234567890")
    accounts_add_parser.add_argument("--proxy", default=None, help="Telegram proxy URL")
    accounts_add_parser.add_argument("--disabled", action="store_true")
    accounts_add_parser.add_argument("--replace", action="store_true", help="Update an existing account")

    accounts_remove_parser = accounts_subparsers.add_parser("remove", help="Remove an account")
    accounts_remove_parser.add_argument("name")

    accounts_enable_parser = accounts_subparsers.add_parser("enable", help="Enable an account")
    accounts_enable_parser.add_argument("name")

    accounts_disable_parser = accounts_subparsers.add_parser("disable", help="Disable an account")
    accounts_disable_parser.add_argument("name")

    accounts_phone_parser = accounts_subparsers.add_parser("phone", help="Set or remove an account phone")
    accounts_phone_subparsers = accounts_phone_parser.add_subparsers(dest="phone_action", required=True)
    accounts_phone_set_parser = accounts_phone_subparsers.add_parser("set", help="Set account phone")
    accounts_phone_set_parser.add_argument("name")
    accounts_phone_set_parser.add_argument("phone")
    accounts_phone_remove_parser = accounts_phone_subparsers.add_parser("remove", help="Remove account phone")
    accounts_phone_remove_parser.add_argument("name")

    accounts_proxy_parser = accounts_subparsers.add_parser("proxy", help="Set or remove an account proxy")
    accounts_proxy_subparsers = accounts_proxy_parser.add_subparsers(dest="proxy_action", required=True)
    accounts_proxy_set_parser = accounts_proxy_subparsers.add_parser("set", help="Set account proxy")
    accounts_proxy_set_parser.add_argument("name")
    accounts_proxy_set_parser.add_argument("proxy")
    accounts_proxy_remove_parser = accounts_proxy_subparsers.add_parser("remove", help="Remove account proxy")
    accounts_proxy_remove_parser.add_argument("name")

    serve_parser = subparsers.add_parser("serve", help="Run the HTTP API")
    add_common_cli_args(serve_parser, suppress_defaults=True)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--cors", action="store_true")
    add_serve_security_cli_args(serve_parser)

    ask_parser = subparsers.add_parser("ask", help="Send one request from the command line")
    add_common_cli_args(ask_parser, suppress_defaults=True)
    ask_parser.add_argument("text")
    ask_parser.add_argument("--file", action="append", default=[])
    ask_parser.add_argument("--account", default=None)
    ask_parser.add_argument("--reset-context", action="store_true")
    ask_parser.add_argument("--send-mode", choices=["full_messages", "last_user"], default="full_messages")

    return parser


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def main() -> None:
    configure_stdio()
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "accounts":
        run_accounts_command(args)
        return

    settings = settings_from_args(args)
    validate_proxy_accounts(settings)

    if args.command == "init-sessions":
        asyncio.run(init_sessions(settings, start_bot=not args.no_start_bot))
        return

    if args.command == "ask":
        asyncio.run(ask_once(settings, args))
        return

    if args.command == "serve":
        import uvicorn

        validate_serve_security(settings, args.host)
        app = build_app(settings)
        uvicorn.run(app, host=args.host, port=args.port)
        return


if __name__ == "__main__":
    main()
