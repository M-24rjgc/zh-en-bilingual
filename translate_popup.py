#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中英双语气泡工具（全局热键版）。

按下全局热键弹出输入框：
  * 输入里含中文 -> 输出「英文译文」+ 一次完全独立的「回译中文」
  * 输入是英文   -> 只输出「中文译文」
翻译忠实保留语气词与原始结构，不做提示词优化。主要输出自动进剪贴板。

命令行：
  python translate_popup.py              # 后台常驻，等热键
  python translate_popup.py --probe      # 按键探测：按一下 Copilot 键看它发什么
  python translate_popup.py --install-startup
  python translate_popup.py --uninstall-startup
  python translate_popup.py --once       # 从 stdin 读文本，打印 JSON 结果（自测用）
  python translate_popup.py --selftest   # 只跑语言判定自测，不联网
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_DIR = os.path.join(APP_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "translate.log")
DEFAULT_CODEX_CONFIG = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")

MUTEX_NAME = "Local\\zh_en_bilingual_popup_v1"

WM_HOTKEY = 0x0312
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WH_KEYBOARD_LL = 13

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

CREATE_NO_WINDOW = 0x08000000

DEFAULT_CONFIG = {
    "hotkey": "win+shift+f23",
    "fallback_hotkey": "ctrl+alt+z",
    "model": "deepseek-flash",
    "base_url": "https://api.deepseek.com",
    "api_key": "",
    "codex_config_path": DEFAULT_CODEX_CONFIG,
    "reasoning_effort": "none",
    "max_tokens": 2000,
    "timeout_sec": 60,
    "auto_copy_english": True,
    "always_on_top": True,
    "font_size": 14,
}

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3000-\u303f]")

SYS_ZH2EN = """You are a faithful Chinese-to-English translation engine.
Rules:
- Preserve the author's tone, register, emotion, filler words and hesitations (for example: 那个, 就是, 反正, emmm, 你懂我意思吧), slang, repeated words and the original sentence order.
- Do NOT polish, optimize, shorten, restructure, "improve", or rewrite the text for clarity.
- Do NOT add, remove, explain, summarize, or comment. Output ONLY the English translation.
- Keep code blocks, inline code, JSON, YAML, XML, URLs, file paths, variable/function names, model names, placeholders, numbers and markdown markers exactly as they are; translate only the natural language around them.
- Preserve line breaks and paragraph structure exactly."""

SYS_EN2ZH = """You are a native Chinese reader with no access to any source text. Translate the given English into natural, faithful Chinese.
Rules:
- Stay close to the English: preserve tone, register, emotion, filler words, hesitations, slang and the original sentence order.
- Do NOT polish, optimize, shorten, restructure, summarize, explain, or add anything.
- Output ONLY the Chinese translation.
- Keep code blocks, inline code, JSON, YAML, XML, URLs, file paths, variable/function names, model names, placeholders, numbers and markdown markers exactly as they are; translate only the natural language around them.
- Preserve line breaks and paragraph structure exactly."""


class TranslationError(RuntimeError):
    """翻译/接口错误，retryable 表示重试一次是否有意义。"""

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------


def setup_logging(verbose_console: bool = False) -> logging.Logger:
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 2 * 1024 * 1024:
            backup = LOG_PATH + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(LOG_PATH, backup)
    except OSError:
        pass
    logger = logging.getLogger("zh_en_bilingual")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        if verbose_console:
            console = logging.StreamHandler(sys.stdout)
            console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(console)
    return logger


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                user_cfg = json.load(fh)
            if isinstance(user_cfg, dict):
                cfg.update({k: v for k, v in user_cfg.items() if v is not None})
        except (OSError, json.JSONDecodeError) as exc:
            raise TranslationError(f"config.json 读取失败: {exc}")
    return cfg


def save_config(cfg: dict) -> None:
    clean = {k: cfg.get(k, DEFAULT_CONFIG.get(k)) for k in DEFAULT_CONFIG}
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


# --------------------------------------------------------------------------
# 热键解析
# --------------------------------------------------------------------------

_VK_SPECIAL = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "delete": 0x2E,
    "`": 0xC0,
    "-": 0xBD,
    "=": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    "\\": 0xDC,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
    "numpad0": 0x60,
    "numpad1": 0x61,
    "numpad2": 0x62,
    "numpad3": 0x63,
    "numpad4": 0x64,
    "numpad5": 0x65,
    "numpad6": 0x66,
    "numpad7": 0x67,
    "numpad8": 0x68,
    "numpad9": 0x69,
}
for _i in range(1, 25):
    _VK_SPECIAL[f"f{_i}"] = 0x6F + _i

_VK_NAMES: dict[int, str] = {}
for _name, _code in _VK_SPECIAL.items():
    _VK_NAMES.setdefault(_code, _name)
for _c in range(ord("A"), ord("Z") + 1):
    _VK_NAMES.setdefault(_c, chr(_c).lower())
for _d in range(ord("0"), ord("9") + 1):
    _VK_NAMES.setdefault(_d, chr(_d))

_MOD_TOKENS = {
    "win": ("win", MOD_WIN),
    "windows": ("win", MOD_WIN),
    "meta": ("win", MOD_WIN),
    "super": ("win", MOD_WIN),
    "cmd": ("win", MOD_WIN),
    "ctrl": ("ctrl", MOD_CONTROL),
    "control": ("ctrl", MOD_CONTROL),
    "shift": ("shift", MOD_SHIFT),
    "alt": ("alt", MOD_ALT),
    "menu": ("alt", MOD_ALT),
}


def vk_name(vk: int) -> str:
    return _VK_NAMES.get(vk, f"0x{vk:02x}")


def _vk_from_token(token: str) -> int:
    tok = token.strip().lower()
    if tok in _VK_SPECIAL:
        return _VK_SPECIAL[tok]
    if tok.startswith("0x"):
        return int(tok, 16)
    if len(tok) == 1:
        ch = tok.upper()
        if ("A" <= ch <= "Z") or ("0" <= ch <= "9"):
            return ord(ch)
    raise ValueError(f"认不出的按键: {token}")


def parse_hotkey(spec: str) -> tuple[int, int, str]:
    """返回 (modifiers, vk, 规范化写法)。"""
    if not spec or not spec.strip():
        raise ValueError("热键为空")
    mods = 0
    vk = None
    names: list[str] = []
    for token in re.split(r"[\s+\-]+", spec.strip()):
        if not token:
            continue
        low = token.lower()
        if low in _MOD_TOKENS:
            name, bit = _MOD_TOKENS[low]
            mods |= bit
            names.append(name)
        else:
            if vk is not None:
                raise ValueError(f"热键里有多个主键: {spec}")
            vk = _vk_from_token(low)
            names.append(vk_name(vk))
    if vk is None:
        raise ValueError(f"热键里没有主键: {spec}")
    return mods, vk, "+".join(names)


def describe_foreground_mods() -> str:
    user32 = ctypes.windll.user32
    VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C
    parts = []
    if user32.GetAsyncKeyState(VK_CONTROL) & 0x8000:
        parts.append("ctrl")
    if user32.GetAsyncKeyState(VK_MENU) & 0x8000:
        parts.append("alt")
    if user32.GetAsyncKeyState(VK_SHIFT) & 0x8000:
        parts.append("shift")
    if (user32.GetAsyncKeyState(VK_LWIN) & 0x8000) or (user32.GetAsyncKeyState(VK_RWIN) & 0x8000):
        parts.append("win")
    return "+".join(parts)


# --------------------------------------------------------------------------
# Win32 底层封装
# --------------------------------------------------------------------------


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, ctypes.c_ssize_t)


def _prepare_winapi() -> tuple:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
    user32.UnhookWindowsHookEx.restype = wt.BOOL
    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, ctypes.c_ssize_t]
    user32.RegisterHotKey.restype = wt.BOOL
    user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
    user32.UnregisterHotKey.restype = wt.BOOL
    user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
    user32.GetMessageW.restype = ctypes.c_int
    user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
    user32.PostThreadMessageW.restype = wt.BOOL
    user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    kernel32.GetModuleHandleW.restype = wt.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    kernel32.CreateMutexW.restype = wt.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    kernel32.GetCurrentThreadId.restype = wt.DWORD
    return user32, kernel32


def acquire_single_instance():
    """已经有一个实例在跑就返回 None。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wt.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return None
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 热键管理线程
# --------------------------------------------------------------------------


class HotkeyManager(threading.Thread):
    """在独立线程里注册热键（或退回低级键盘钩子），命中后回调 on_fire。"""

    def __init__(self, spec: str, fallback_spec: str, on_fire, on_info, logger: logging.Logger):
        super().__init__(daemon=True, name="hotkey")
        self.spec = spec
        self.fallback_spec = fallback_spec
        self.on_fire = on_fire
        self.on_info = on_info
        self.logger = logger
        self.mode = "pending"
        self.mode_label = spec
        self.user32 = None
        self.kernel32 = None
        self.thread_id = 0
        self.hook_handle = None
        self._hook_proc_ref = None
        self.hook_vk = 0
        self._key_down = False
        self._stop = threading.Event()

    # -- 生命周期 ---------------------------------------------------------
    def run(self) -> None:
        self.user32, self.kernel32 = _prepare_winapi()
        self.thread_id = self.kernel32.GetCurrentThreadId()
        try:
            mods, vk, canon = parse_hotkey(self.spec)
        except ValueError as exc:
            self.mode = "invalid"
            self.on_info("invalid", f"热键 {self.spec} 无法解析：{exc}")
            return

        if self.user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk):
            self.mode = "hotkey"
            self.mode_label = canon
            self.on_info("hotkey", canon)
        else:
            err = ctypes.get_last_error()
            self.logger.warning("RegisterHotKey(%s) 失败，错误码 %s，改用低级键盘钩子", canon, err)
            if self._install_hook(vk):
                self.mode = "hook"
                self.mode_label = canon
                self.on_info("hook", canon)
            else:
                try:
                    fmods, fvk, fcanon = parse_hotkey(self.fallback_spec)
                except ValueError:
                    self.mode = "failed"
                    self.on_info("failed", "热键注册失败，备用热键也无法解析")
                    return
                if self.user32.RegisterHotKey(None, 2, fmods | MOD_NOREPEAT, fvk):
                    self.mode = "fallback"
                    self.mode_label = fcanon
                    self.on_info("fallback", fcanon)
                else:
                    self.mode = "failed"
                    self.on_info("failed", f"热键注册失败，备用热键 {fcanon} 也被占用")
                    return

        msg = wt.MSG()
        while not self._stop.is_set():
            ret = self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            if msg.message == WM_HOTKEY:
                self.logger.info("热键触发 (%s)", self.mode_label)
                self.on_fire()
            else:
                self.user32.TranslateMessage(ctypes.byref(msg))
                self.user32.DispatchMessageW(ctypes.byref(msg))

    def stop(self) -> None:
        self._stop.set()
        if self.thread_id:
            try:
                self.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)  # WM_QUIT
            except Exception:
                pass

    # -- 低级键盘钩子 -----------------------------------------------------
    def _install_hook(self, vk: int) -> bool:
        self.hook_vk = vk
        proc = HOOKPROC(self._hook_cb)
        hinst = self.kernel32.GetModuleHandleW(None)
        handle = self.user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc, hinst, 0)
        if not handle:
            self.logger.warning("SetWindowsHookExW 失败，错误码 %s", ctypes.get_last_error())
            return False
        self.hook_handle = handle
        self._hook_proc_ref = proc
        return True

    def _hook_cb(self, ncode, wparam, lparam):
        try:
            if ncode == 0 and lparam:
                kb = ctypes.cast(ctypes.c_void_p(lparam), ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if kb.vkCode == self.hook_vk:
                    if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        if not self._key_down:
                            self._key_down = True
                            self.logger.info("钩子命中 (%s)", self.mode_label)
                            self.on_fire()
                    elif wparam in (WM_KEYUP, WM_SYSKEYUP):
                        self._key_down = False
                    return 1
        except Exception:
            self.logger.exception("钩子回调异常")
        return self.user32.CallNextHookEx(None, ncode, wparam, lparam)


# --------------------------------------------------------------------------
# 翻译
# --------------------------------------------------------------------------


def has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text))


def detect_direction(text: str) -> str:
    """含中文 -> zh2en（双语）；否则 -> en2zh（只给中文）。"""
    return "zh2en" if has_cjk(text) else "en2zh"


def _read_codex_credentials(cfg: dict) -> tuple[str, str]:
    path = cfg.get("codex_config_path") or DEFAULT_CODEX_CONFIG
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError as exc:
        return "", ""
    key = ""
    base = ""
    match = re.search(r'experimental_bearer_token\s*=\s*"([^"]+)"', raw)
    if match:
        key = match.group(1).strip()
    match = re.search(r'base_url\s*=\s*"([^"]+)"', raw)
    if match:
        base = match.group(1).strip().rstrip("/")
    return key, base


def resolve_api(cfg: dict) -> tuple[str, str]:
    key = str(cfg.get("api_key") or "").strip()
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    if not key:
        file_key, file_base = _read_codex_credentials(cfg)
        key = file_key
        if not base:
            base = file_base
    if not key:
        raise TranslationError("没有可用的 API key：config.json 的 api_key 为空，也没能从 Codex 配置里读到 token")
    if not base:
        base = "https://api.deepseek.com"
    return key, base


def _post_chat(effort: str | None, api: tuple[str, str], cfg: dict, messages: list, max_tokens: int) -> str:
    key, base = api
    url = base + "/chat/completions"
    timeout = float(cfg.get("timeout_sec") or 60)
    payload = {
        "model": str(cfg.get("model") or "deepseek-flash"),
        "messages": messages,
        "max_tokens": int(max_tokens),
    }
    if effort:
        payload["reasoning_effort"] = effort
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        retryable = exc.code >= 500 or exc.code == 429
        raise TranslationError(f"接口返回 HTTP {exc.code}：{detail}", retryable=retryable) from exc
    except urllib.error.URLError as exc:
        raise TranslationError(f"网络错误：{exc.reason}", retryable=True) from exc
    except TimeoutError as exc:
        raise TranslationError(f"请求超时（>{timeout:g}s）", retryable=True) from exc
    except OSError as exc:
        raise TranslationError(f"网络异常：{exc}", retryable=True) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TranslationError(f"返回值不是合法 JSON：{raw[:200]}") from exc
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise TranslationError(f"返回值里没有译文：{raw[:200]}")
    return (content or "").strip()


def call_model(api: tuple[str, str], cfg: dict, messages: list, logger: logging.Logger) -> str:
    effort = str(cfg.get("reasoning_effort") or "").strip()
    max_tokens = int(cfg.get("max_tokens") or 2000)
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            out = _post_chat(effort, api, cfg, messages, max_tokens)
        except TranslationError as exc:
            if exc.retryable and attempt == 0:
                logger.warning("可重试错误，0.8s 后重试：%s", exc)
                time.sleep(0.8)
                last_error = exc
                continue
            raise
        if out:
            return out
        if attempt == 0:
            logger.warning("模型只返回了思考内容、正文为空，改用关闭推理 + 更大 max_tokens 重试")
            effort = ""
            max_tokens = max(max_tokens, 4000)
            continue
        raise TranslationError("模型返回了空内容（可能是 token 上限太小）")
    raise TranslationError(str(last_error) if last_error else "翻译失败")


def translate(text: str, cfg: dict, logger: logging.Logger, progress=None) -> dict:
    text = (text or "").strip()
    if not text:
        raise TranslationError("输入是空的")
    if len(text) > 40000:
        raise TranslationError(f"输入太长了（{len(text)} 字符），超过 40000 字符上限")
    api = resolve_api(cfg)
    direction = detect_direction(text)
    started = time.perf_counter()
    if direction == "zh2en":
        if progress:
            progress("正在翻译成英文…")
        english = call_model(api, cfg, [
            {"role": "system", "content": SYS_ZH2EN},
            {"role": "user", "content": text},
        ], logger)
        if progress:
            progress("正在做独立的回译中文（只看英文）…")
        chinese = call_model(api, cfg, [
            {"role": "system", "content": SYS_EN2ZH},
            {"role": "user", "content": english},
        ], logger)
    else:
        if progress:
            progress("正在翻译成中文…")
        english = ""
        chinese = call_model(api, cfg, [
            {"role": "system", "content": SYS_EN2ZH},
            {"role": "user", "content": text},
        ], logger)
    elapsed = time.perf_counter() - started
    logger.info(
        "翻译完成 direction=%s chars=%d english_len=%d chinese_len=%d elapsed=%.2fs",
        direction, len(text), len(english), len(chinese), elapsed,
    )
    return {
        "direction": direction,
        "english": english,
        "chinese": chinese,
        "elapsed": elapsed,
        "chars": len(text),
    }


# --------------------------------------------------------------------------
# 弹窗界面
# --------------------------------------------------------------------------


class PopupApp:
    def __init__(self, root, cfg: dict, logger: logging.Logger):
        import tkinter as tk
        from tkinter import messagebox

        self.tk = tk
        self.messagebox = messagebox
        self.root = root
        self.cfg = cfg
        self.logger = logger
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.hotkey_text = cfg.get("hotkey", "")

        size = int(cfg.get("font_size") or 14)
        family = "Microsoft YaHei UI"
        self.ui_font = (family, size)
        self.small_font = (family, max(9, size - 2))
        self.mono_font = ("Consolas", max(10, size - 1))

        root.title("中英双语气泡")
        root.geometry("780x860")
        root.minsize(620, 640)
        root.protocol("WM_DELETE_WINDOW", self.hide)
        root.configure(bg="#f4f5f7")

        self._build_widgets()
        root.bind("<Control-Return>", self._on_ctrl_enter)
        root.bind("<Escape>", lambda _e: self.hide())
        root.withdraw()

    # -- 界面 -------------------------------------------------------------
    def _build_widgets(self) -> None:
        tk = self.tk
        outer = tk.Frame(self.root, bg="#f4f5f7", padx=12, pady=12)
        outer.pack(fill="both", expand=True)

        tk.Label(
            outer,
            text="输入中文 → 英文译文 + 独立回译中文；输入英文 → 只给中文译文",
            bg="#f4f5f7",
            anchor="w",
            font=self.small_font,
        ).pack(fill="x")

        self.input = tk.Text(outer, height=9, wrap="word", undo=True, font=self.ui_font, relief="solid", borderwidth=1)
        self.input.pack(fill="both", expand=True, pady=(6, 8))

        buttons = tk.Frame(outer, bg="#f4f5f7")
        buttons.pack(fill="x")
        self.btn_translate = tk.Button(buttons, text="翻译 (Ctrl+Enter)", command=self.start_translate, font=self.small_font)
        self.btn_translate.pack(side="left")
        tk.Button(buttons, text="复制英文", command=lambda: self.copy_field("english"), font=self.small_font).pack(side="left", padx=(8, 0))
        tk.Button(buttons, text="复制回译中文", command=lambda: self.copy_field("chinese"), font=self.small_font).pack(side="left", padx=(8, 0))
        tk.Button(buttons, text="复制全部", command=self.copy_all, font=self.small_font).pack(side="left", padx=(8, 0))
        tk.Button(buttons, text="清空", command=self.clear_all, font=self.small_font).pack(side="left", padx=(8, 0))
        tk.Button(buttons, text="关闭 (Esc)", command=self.hide, font=self.small_font).pack(side="right")

        tk.Label(outer, text="English", bg="#f4f5f7", anchor="w", font=self.small_font).pack(fill="x", pady=(12, 2))
        self.out_en = tk.Text(outer, height=7, wrap="word", font=self.mono_font, relief="solid", borderwidth=1, bg="#ffffff")
        self.out_en.pack(fill="both", expand=True)

        tk.Label(outer, text="回译中文", bg="#f4f5f7", anchor="w", font=self.small_font).pack(fill="x", pady=(10, 2))
        self.out_zh = tk.Text(outer, height=6, wrap="word", font=self.ui_font, relief="solid", borderwidth=1, bg="#ffffff")
        self.out_zh.pack(fill="both", expand=True)

        self.status = tk.Label(outer, text="", bg="#f4f5f7", anchor="w", font=self.small_font, fg="#444444")
        self.status.pack(fill="x", pady=(8, 0))

        for widget in (self.out_en, self.out_zh):
            widget.configure(state="disabled")

    # -- 显示 / 隐藏 -------------------------------------------------------
    def show(self) -> None:
        self.root.attributes("-topmost", True)
        self.root.deiconify()
        try:
            self.root.attributes("-topmost", bool(self.cfg.get("always_on_top", True)))
        except Exception:
            pass
        self._move_near_cursor()
        self.root.lift()
        self._force_foreground()
        try:
            self.root.focus_force()
        except Exception:
            pass
        self.input.focus_set()
        self.root.after(120, lambda: self.input.focus_force())

    def _force_foreground(self) -> None:
        """Windows 有时不允许后台进程抢焦点，这里用原生 API 再推一把。"""
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            user32.ShowWindow(hwnd, 5)  # SW_SHOW
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def hide(self) -> None:
        self.root.withdraw()

    def _move_near_cursor(self) -> None:
        try:
            self.root.update_idletasks()
            width = self.root.winfo_width() or 780
            height = self.root.winfo_height() or 860
            x = self.root.winfo_pointerx() - width // 2
            y = self.root.winfo_pointery() - 80
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            x = max(0, min(x, screen_w - width))
            y = max(0, min(y, screen_h - height))
            self.root.geometry(f"+{x}+{y}")
        except Exception:
            pass

    # -- 事件循环 ---------------------------------------------------------
    def poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "fire":
                    self.show()
                elif kind == "status":
                    self.set_status(payload)
                elif kind == "done":
                    self.on_done(payload)
                elif kind == "error":
                    self.on_error(payload)
                elif kind == "hotkey":
                    self.on_hotkey_info(payload[0], payload[1])
        except queue.Empty:
            pass
        self.root.after(80, self.poll)

    def on_hotkey_info(self, mode: str, detail: str) -> None:
        if mode == "hotkey":
            text = f"热键已接管：{detail}（Copilot 键不再启动 Copilot）"
        elif mode == "hook":
            text = f"热键由键盘钩子接管：{detail}"
        elif mode == "fallback":
            text = f"原热键不可用，已改用备用热键：{detail}"
        elif mode == "invalid":
            text = f"热键配置有问题：{detail}"
        else:
            text = f"热键注册失败：{detail}"
        self.hotkey_text = detail
        self.logger.info("热键状态 %s: %s", mode, detail)
        self.set_status(text)
        if mode in ("fallback", "failed", "invalid"):
            self.show()
            self.messagebox.showwarning("中英双语气泡", text)

    # -- 交互 -------------------------------------------------------------
    def set_status(self, text: str, color: str = "#444444") -> None:
        self.status.configure(text=text, fg=color)

    def _on_ctrl_enter(self, _event):
        self.start_translate()
        return "break"

    def start_translate(self) -> None:
        if self.busy:
            return
        text = self.input.get("1.0", "end").strip()
        if not text:
            self.set_status("先输入要翻译的内容", "#b00020")
            return
        self.busy = True
        self.btn_translate.configure(state="disabled")
        self.set_text(self.out_en, "")
        self.set_text(self.out_zh, "")
        self.set_status("翻译中…")
        threading.Thread(target=self._worker, args=(text,), daemon=True, name="translate").start()

    def _worker(self, text: str) -> None:
        try:
            result = translate(text, self.cfg, self.logger, progress=lambda msg: self.events.put(("status", msg)))
            self.events.put(("done", result))
        except TranslationError as exc:
            self.logger.warning("翻译失败：%s", exc)
            self.events.put(("error", str(exc)))
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("翻译异常")
            self.events.put(("error", f"{type(exc).__name__}: {exc}"))

    def on_done(self, result: dict) -> None:
        self.busy = False
        self.btn_translate.configure(state="normal")
        self.set_text(self.out_en, result["english"])
        self.set_text(self.out_zh, result["chinese"])
        if result["direction"] == "zh2en":
            direction_text = "中文 → 英文 + 独立回译"
            primary = result["english"]
            primary_name = "英文译文"
        else:
            direction_text = "英文 → 中文"
            primary = result["chinese"]
            primary_name = "中文译文"
        copied = ""
        if self.cfg.get("auto_copy_english", True) and primary:
            self.copy_to_clipboard(primary)
            copied = f"，{primary_name}已复制到剪贴板"
        self.set_status(
            f"{direction_text} | 原文 {result['chars']} 字符 | 用时 {result['elapsed']:.1f}s{copied}",
            "#0b6b3a",
        )

    def on_error(self, message: str) -> None:
        self.busy = False
        self.btn_translate.configure(state="normal")
        self.set_status("失败：" + message, "#b00020")

    # -- 工具 -------------------------------------------------------------
    def set_text(self, widget, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def get_text(self, widget) -> str:
        return widget.get("1.0", "end").strip()

    def copy_to_clipboard(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()

    def copy_field(self, field: str) -> None:
        widget = self.out_en if field == "english" else self.out_zh
        text = self.get_text(widget)
        if not text:
            self.set_status("这块还是空的", "#b00020")
            return
        self.copy_to_clipboard(text)
        self.set_status("已复制", "#0b6b3a")

    def copy_all(self) -> None:
        english = self.get_text(self.out_en)
        chinese = self.get_text(self.out_zh)
        if not english and not chinese:
            self.set_status("还没有结果", "#b00020")
            return
        parts = []
        if english:
            parts.append("English:\n" + english)
        if chinese:
            parts.append("回译中文:\n" + chinese)
        self.copy_to_clipboard("\n\n".join(parts))
        self.set_status("已复制全部", "#0b6b3a")

    def clear_all(self) -> None:
        self.input.delete("1.0", "end")
        self.set_text(self.out_en, "")
        self.set_text(self.out_zh, "")
        self.set_status("")
        self.input.focus_set()


# --------------------------------------------------------------------------
# 按键探测模式
# --------------------------------------------------------------------------


def run_probe(cfg: dict, logger: logging.Logger) -> int:
    import tkinter as tk
    from tkinter import messagebox

    enable_dpi_awareness()
    user32, kernel32 = _prepare_winapi()

    root = tk.Tk()
    root.title("按键探测（按一下 Copilot 键）")
    root.geometry("560x420")
    font_big = ("Microsoft YaHei UI", 16, "bold")
    font_small = ("Microsoft YaHei UI", 11)

    tk.Label(root, text="现在按一下你要用的那个键（Copilot 键）", font=font_big).pack(pady=(16, 6))
    tk.Label(root, text="下面会显示它实际发出的键码；如果一直没反应，说明这个键被系统/驱动截走了。", font=font_small, wraplength=520).pack(pady=(0, 10))
    detected = tk.StringVar(value="等待按键…")
    tk.Label(root, textvariable=detected, font=("Consolas", 15), fg="#0b6b3a").pack(pady=6)
    log_box = tk.Text(root, height=8, font=("Consolas", 10), wrap="word")
    log_box.pack(fill="both", expand=True, padx=12)
    log_box.configure(state="disabled")

    state = {"spec": "", "vk": 0}

    def append(line: str) -> None:
        log_box.configure(state="normal")
        log_box.insert("end", line + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")

    def on_key(vk: int, mods: str) -> None:
        spec = (mods + "+" if mods else "") + vk_name(vk)
        state["spec"] = spec
        state["vk"] = vk
        detected.set(f"检测到：{spec}   (vk=0x{vk:02x})")
        append(f"{time.strftime('%H:%M:%S')}  {spec}  vk=0x{vk:02x}")

    proc_holder = {}

    def hook_cb(ncode, wparam, lparam):
        try:
            if ncode == 0 and lparam and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                kb = ctypes.cast(ctypes.c_void_p(lparam), ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if kb.vkCode not in (0x5B, 0x5C, 0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5):
                    root.after(0, on_key, kb.vkCode, describe_foreground_mods())
        except Exception:
            pass
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    proc = HOOKPROC(hook_cb)
    proc_holder["proc"] = proc
    handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc, kernel32.GetModuleHandleW(None), 0)
    if not handle:
        messagebox.showerror("按键探测", f"键盘钩子安装失败，错误码 {ctypes.get_last_error()}")
        return 1
    append("键盘钩子已安装，开始监听…")

    try:
        mods, vk, canon = parse_hotkey(cfg.get("hotkey", ""))
        if user32.RegisterHotKey(None, 9, mods | MOD_NOREPEAT, vk):
            user32.UnregisterHotKey(None, 9)
            append(f"当前配置的热键 {canon} 可以注册成功。")
        else:
            append(f"当前配置的热键 {canon} 注册失败（错误码 {ctypes.get_last_error()}）；若钩子能抓到它仍可用。")
    except ValueError as exc:
        append(f"当前配置的热键无法解析：{exc}")

    def save_hotkey() -> None:
        if not state["spec"]:
            messagebox.showinfo("按键探测", "还没检测到按键，先按一下要用的键。")
            return
        cfg["hotkey"] = state["spec"]
        save_config(cfg)
        append(f"已把热键写入 config.json：{state['spec']}（重启程序后生效）")
        messagebox.showinfo("按键探测", f"已保存热键 {state['spec']}。\n重启中英双语气泡后生效。")

    buttons = tk.Frame(root)
    buttons.pack(fill="x", pady=10, padx=12)
    tk.Button(buttons, text="用这个组合键作为热键并保存", command=save_hotkey, font=font_small).pack(side="left")
    tk.Button(buttons, text="退出", command=root.destroy, font=font_small).pack(side="right")

    def cleanup() -> None:
        user32.UnhookWindowsHookEx(handle)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", cleanup)
    root.mainloop()
    try:
        user32.UnhookWindowsHookEx(handle)
    except Exception:
        pass
    return 0


# --------------------------------------------------------------------------
# 开机自启
# --------------------------------------------------------------------------


def startup_lnk_path() -> str:
    appdata = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "zh-en-bilingual.lnk")


def _pythonw_path() -> str:
    candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return candidate if os.path.exists(candidate) else sys.executable


def install_startup(logger: logging.Logger) -> int:
    lnk = startup_lnk_path()
    script = os.path.join(APP_DIR, "translate_popup.py")
    ps = (
        "$ws = New-Object -ComObject WScript.Shell\n"
        f"$sc = $ws.CreateShortcut('{lnk}')\n"
        f"$sc.TargetPath = '{_pythonw_path()}'\n"
        f"$sc.Arguments = '\"{script}\"'\n"
        f"$sc.WorkingDirectory = '{APP_DIR}'\n"
        "$sc.WindowStyle = 7\n"
        "$sc.Description = 'zh-en bilingual popup'\n"
        "$sc.Save()\n"
    )
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0 or not os.path.exists(lnk):
        print("创建开机启动快捷方式失败：")
        print(result.stdout)
        print(result.stderr)
        logger.error("install_startup failed: %s %s", result.stdout, result.stderr)
        return 1
    print(f"已创建开机自启：{lnk}")
    print(f"  目标：{_pythonw_path()} \"{script}\"")
    logger.info("install_startup ok -> %s", lnk)
    return 0


def uninstall_startup(logger: logging.Logger) -> int:
    lnk = startup_lnk_path()
    if os.path.exists(lnk):
        os.remove(lnk)
        print(f"已删除开机自启：{lnk}")
        logger.info("uninstall_startup removed %s", lnk)
    else:
        print("本来就没有开机自启项。")
    return 0


# --------------------------------------------------------------------------
# 命令行入口
# --------------------------------------------------------------------------


def run_once(logger: logging.Logger) -> int:
    text = sys.stdin.read()
    cfg = load_config()
    try:
        result = translate(text, cfg, logger)
    except TranslationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    result["ok"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def run_selftest() -> int:
    cases = [
        ("就是那个，你先看一下，emmm，我觉得还行吧", "zh2en"),
        ("hello, can you take a look at this for me?", "en2zh"),
        ("用 FastAPI 写一个 /health 接口，返回 {\"ok\": true}", "zh2en"),
        ("def main():\n    return 42", "en2zh"),
        ("麻烦把这段 JSON 里的 name 改成 Alice:\n{\"name\": \"Bob\"}", "zh2en"),
        ("", "en2zh"),
    ]
    failed = 0
    for text, expected in cases:
        got = detect_direction(text)
        flag = "OK " if got == expected else "FAIL"
        if got != expected:
            failed += 1
        print(f"{flag} expect={expected} got={got} :: {text[:40]!r}")
    print(f"热键解析: {parse_hotkey('win+shift+f23')}")
    print(f"热键解析: {parse_hotkey('ctrl+alt+z')}")
    return 1 if failed else 0


def run_app(logger: logging.Logger) -> int:
    import tkinter as tk

    cfg = load_config()
    enable_dpi_awareness()
    handle = acquire_single_instance()
    if handle is None:
        print("中英双语气泡已经在运行了（单实例），这次启动直接退出。")
        logger.info("another instance already running, exit")
        return 0

    root = tk.Tk()
    app = PopupApp(root, cfg, logger)

    def on_fire() -> None:
        app.events.put(("fire", None))

    def on_info(mode: str, detail: str) -> None:
        app.events.put(("hotkey", (mode, detail)))

    manager = HotkeyManager(
        spec=str(cfg.get("hotkey") or "win+shift+f23"),
        fallback_spec=str(cfg.get("fallback_hotkey") or "ctrl+alt+z"),
        on_fire=on_fire,
        on_info=on_info,
        logger=logger,
    )
    manager.start()
    logger.info("started pid=%s hotkey=%s", os.getpid(), cfg.get("hotkey"))

    root.after(80, app.poll)
    try:
        root.mainloop()
    finally:
        manager.stop()
        logger.info("exited")
    return 0


def main() -> int:
    args = set(sys.argv[1:])
    verbose = any(a in args for a in ("--once", "--selftest", "--install-startup", "--uninstall-startup", "--probe"))
    logger = setup_logging(verbose_console=verbose)
    try:
        if "--selftest" in args:
            return run_selftest()
        if "--once" in args:
            return run_once(logger)
        if "--probe" in args:
            return run_probe(load_config(), logger)
        if "--install-startup" in args:
            return install_startup(logger)
        if "--uninstall-startup" in args:
            return uninstall_startup(logger)
        return run_app(logger)
    except TranslationError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
