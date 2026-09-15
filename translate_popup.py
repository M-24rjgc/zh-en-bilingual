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
  python translate_popup.py --snapshot   # 渲染各状态的界面截图到 snapshots/（开发验收用）
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
    "theme": "auto",
    "frameless": True,
    "translucency": 0.97,
    "corner_radius": 20,
    "bubble_tail": True,
    "animations": True,
    "auto_dismiss": True,
    "accent": "",
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
# 主题与配色
# --------------------------------------------------------------------------

ACCENT_FALLBACK = "#0078D4"
TRANSPARENT_KEY = "#FF00FE"

LIGHT_TOKENS = {
    "bubble": "#FFFFFF",
    "card": "#F6F8FA",
    "text": "#161A1F",
    "dim": "#5A626B",
    "muted": "#8C949C",
    "border": "#E7EAEF",
    "hairline": "#EEF1F5",
    "chip": "#F1F3F6",
    "chip_text": "#454C54",
    "hover": "#E9EDF2",
    "press": "#D9E0E8",
    "amber": "#E8A33D",
    "ok": "#16A34A",
    "ok_bg": "#E8F5EC",
    "err": "#B00020",
}

DARK_TOKENS = {
    "bubble": "#1F2125",
    "card": "#17191C",
    "text": "#E8EAED",
    "dim": "#A8B0B8",
    "muted": "#79828C",
    "border": "#2E3238",
    "hairline": "#2A2E34",
    "chip": "#262A30",
    "chip_text": "#C3CAD2",
    "hover": "#2C3138",
    "press": "#39404A",
    "amber": "#E0A45C",
    "ok": "#3CCB7F",
    "ok_bg": "#17301F",
    "err": "#FF6B6B",
}


def detect_system_theme() -> str:
    """读系统「应用」配色模式，返回 light 或 dark。"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if int(value) else "dark"
    except Exception:
        return "light"


def detect_accent_color() -> str:
    """读系统强调色（注册表里存的是 ABGR）。"""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM") as key:
            value, _ = winreg.QueryValueEx(key, "AccentColor")
        value = int(value) & 0xFFFFFFFF
        return "#%02X%02X%02X" % (value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)
    except Exception:
        return ACCENT_FALLBACK


def _hex_rgb(color: str) -> tuple:
    color = (color or "").lstrip("#")
    if len(color) != 6:
        color = ACCENT_FALLBACK.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _blend(color: str, other: str, amount: float) -> str:
    a = _hex_rgb(color)
    b = _hex_rgb(other)
    amount = max(0.0, min(1.0, amount))
    return "#%02X%02X%02X" % tuple(int(round(a[i] + (b[i] - a[i]) * amount)) for i in range(3))


def build_tokens(theme: str, accent: str) -> dict:
    tokens = dict(LIGHT_TOKENS if theme == "light" else DARK_TOKENS)
    accent = accent if re.fullmatch(r"#[0-9A-Fa-f]{6}", accent or "") else ACCENT_FALLBACK
    tokens["accent"] = accent.upper()
    tokens["accent_hover"] = _blend(accent, "#000000" if theme == "light" else "#FFFFFF", 0.12)
    tokens["accent_soft"] = _blend(accent, tokens["bubble"], 0.86)
    tokens["select"] = _blend(accent, tokens["bubble"], 0.68)
    return tokens


# --------------------------------------------------------------------------
# 气泡界面（canvas 自绘 + 三个 Text 叠放）
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

        self.unit = max(0.8, min(2.2, float(cfg.get("font_size") or 14) / 14.0))
        try:
            self.dpi = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
        except Exception:
            self.dpi = 1.0
        self.frameless = bool(cfg.get("frameless", True))
        self.animations = bool(cfg.get("animations", True))
        self.auto_dismiss = bool(cfg.get("auto_dismiss", True))
        self.use_tail = bool(cfg.get("bubble_tail", True))
        self.radius = self.px(float(cfg.get("corner_radius") or 20))
        try:
            self.translucency = max(0.55, min(1.0, float(cfg.get("translucency") or 1.0)))
        except (TypeError, ValueError):
            self.translucency = 1.0

        self.theme_name = "light"
        self.tokens = build_tokens("light", ACCENT_FALLBACK)
        self.hotkey_text = str(cfg.get("hotkey") or "")
        self.state = "input"
        self.busy = False
        self.visible = False
        self.pinned = False
        self.hover = False
        self.hover_until = 0.0
        self.was_foreground = False
        self.last_fg_ours = False
        self.shown_at = 0.0
        self.copy_flash_until = 0.0
        self.status_text = "输入中文开始翻译"
        self.status_kind = "idle"
        self.has_result = False
        self.progress = 0.0
        self.win_x = 160
        self.win_y = 160
        self.tail_edge = "top"
        self.tail_x = 0
        self.rects: dict = {}
        self.items: dict = {}
        self._jobs: set = set()
        self.drag_offset = None

        self.ui_family = "Microsoft YaHei UI"
        self.en_family = "Segoe UI Variable Display"
        self.label_family = "Segoe UI Variable Text"
        self.mono_family = "Cascadia Mono"
        self._setup_fonts()
        self._create_widgets()
        self._apply_window_style()
        root.title("中英双语气泡")
        root.protocol("WM_DELETE_WINDOW", self.hide)
        root.withdraw()

    # -- 尺寸与字体 --------------------------------------------------------
    def px(self, value: float) -> int:
        """逻辑像素 -> 真实像素（跟随 DPI 与 font_size）。"""
        return int(round(value * self.unit * self.dpi))

    def _setup_fonts(self) -> None:
        u = self.unit
        self.f_title = (self.ui_family, int(round(12 * u)), "bold")
        self.f_body = (self.ui_family, int(round(11.5 * u)))
        self.f_en = (self.en_family, int(round(12 * u)))
        self.f_label = (self.label_family, int(round(8.5 * u)), "bold")
        self.f_chip = (self.mono_family, int(round(8.5 * u)))
        self.f_status = (self.ui_family, int(round(9.5 * u)))
        self.f_icon = (self.ui_family, int(round(11 * u)), "bold")

    # -- 基础控件 ----------------------------------------------------------
    def _make_text(self, font, undo: bool = False, readonly: bool = False):
        widget = self.tk.Text(
            self.root,
            wrap="word",
            font=font,
            relief="flat",
            bd=0,
            highlightthickness=0,
            undo=undo,
            padx=self.px(10),
            pady=self.px(7),
            spacing1=self.px(1),
            spacing3=self.px(2),
            insertwidth=self.px(2),
        )
        if readonly:
            widget.configure(state="disabled")
        return widget

    def _create_widgets(self) -> None:
        tk = self.tk
        self.canvas = tk.Canvas(self.root, highlightthickness=0, bd=0, bg=TRANSPARENT_KEY)
        self.canvas.pack(fill="both", expand=True)
        self.input = self._make_text(self.f_body, undo=True)
        self.out_en = self._make_text(self.f_en, readonly=True)
        self.out_zh = self._make_text(self.f_body, readonly=True)
        # 占位提示必须是独立控件：Text 是子窗口，会盖住 canvas 上画的东西
        self.placeholder = tk.Label(self.root, text="粘贴或输入中文…", anchor="nw", bd=0,
                                    padx=0, pady=0, justify="left")

        self.canvas.bind("<Button-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<Enter>", lambda _e: self._set_hover(True))
        self.canvas.bind("<Leave>", lambda _e: self._set_hover(False))
        self.root.bind("<Control-Return>", self._on_ctrl_enter)
        self.root.bind("<Escape>", lambda _e: self.hide())
        self.root.bind("<Control-Key-1>", lambda _e: self.copy_field("english"))
        self.root.bind("<Control-Key-2>", lambda _e: self.copy_field("chinese"))
        self.root.bind("<Control-Shift-Key-C>", lambda _e: self.copy_all())
        for widget in (self.canvas, self.input, self.out_en, self.out_zh):
            widget.bind("<MouseWheel>", self._on_wheel)
        self.input.bind("<KeyRelease>", self._on_input_key)
        self.input.bind("<<Paste>>", lambda _e: self._schedule(30, self._on_input_key))

    def _on_input_key(self, _event=None) -> None:
        self._update_placeholder()

    def _update_placeholder(self) -> None:
        if not self.rects:
            return
        if self.input.get("1.0", "end").strip():
            self.placeholder.place_forget()
            return
        rect = self.rects["input_text"]
        self.placeholder.configure(bg=self.tokens["card"], fg=self.tokens["muted"], font=self.f_body)
        self.placeholder.place(x=rect[0] + self.px(2), y=rect[1] + self.px(2))
        self.placeholder.lift()

    def _apply_window_style(self) -> None:
        try:
            self.root.attributes("-topmost", bool(self.cfg.get("always_on_top", True)))
        except Exception:
            pass
        if self.frameless:
            self.root.overrideredirect(True)
            try:
                self.root.attributes("-transparentcolor", TRANSPARENT_KEY)
            except Exception:
                self.logger.warning("透明色设置失败，气泡将带底色")
        if self.translucency < 1.0:
            try:
                self.root.attributes("-alpha", self.translucency)
            except Exception:
                pass

    def _refresh_theme(self) -> None:
        mode = str(self.cfg.get("theme") or "auto").lower()
        if mode not in ("light", "dark"):
            mode = detect_system_theme()
        accent = str(self.cfg.get("accent") or "").strip() or detect_accent_color()
        self.theme_name = mode
        self.tokens = build_tokens(mode, accent)

    # -- 布局 --------------------------------------------------------------
    def _compute_layout(self) -> dict:
        w = self.px(660)
        pad = self.px(16)
        head = self.px(34)
        gap = self.px(9)
        footer = self.px(34)
        tail = self.px(13) if (self.frameless and self.use_tail) else 0
        top_tail = tail if self.tail_edge == "top" else 0
        bottom_tail = tail if self.tail_edge == "bottom" else 0
        input_h = self.px(116) if self.state in ("input", "loading") else self.px(100)
        en_h = self.px(126)
        zh_h = self.px(102)

        rects: dict = {"width": w, "top_tail": top_tail, "bottom_tail": bottom_tail}
        y = top_tail + self.px(14)
        rects["header"] = (pad, y, w - pad, y + head)
        icon = self.px(26)
        rects["icon"] = (pad, y + self.px(4), pad + icon, y + self.px(4) + icon)
        rects["title"] = (pad + icon + self.px(10), y, w - pad - self.px(158), y + head)
        rects["close"] = (w - pad - self.px(22), y + self.px(6), w - pad, y + head - self.px(6))
        chip_w = self.px(104)
        rects["chip"] = (
            rects["close"][0] - self.px(8) - chip_w, y + self.px(7),
            rects["close"][0] - self.px(8), y + head - self.px(7),
        )
        y += head + gap

        rects["input_card"] = (pad, y, w - pad, y + input_h)
        rects["input_text"] = (pad + self.px(6), y + self.px(4), w - pad - self.px(6), y + input_h - self.px(20))
        rects["hint"] = (pad, y + input_h - self.px(20), w - pad - self.px(10), y + input_h - self.px(4))
        y += input_h + gap

        if self.has_result:
            rects["en_card"] = (pad, y, w - pad, y + en_h)
            rects["en_rail"] = (pad + self.px(1), y + self.px(14), pad + self.px(4), y + en_h - self.px(14))
            rects["en_label"] = (pad + self.px(16), y + self.px(9), w - pad, y + self.px(24))
            rects["en_text"] = (pad + self.px(10), y + self.px(22), w - pad - self.px(8), y + en_h - self.px(4))
            y += en_h + gap
            rects["zh_card"] = (pad, y, w - pad, y + zh_h)
            rects["zh_rail"] = (pad + self.px(1), y + self.px(14), pad + self.px(4), y + zh_h - self.px(14))
            rects["zh_label"] = (pad + self.px(16), y + self.px(9), w - pad, y + self.px(24))
            rects["zh_text"] = (pad + self.px(10), y + self.px(22), w - pad - self.px(8), y + zh_h - self.px(4))
            y += zh_h + gap

        rects["footer"] = (pad, y, w - pad, y + footer)
        height = y + footer + self.px(14) + bottom_tail
        rects["bubble"] = (0, top_tail, w, height - bottom_tail)
        rects["height"] = height
        return rects

    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        r = max(1, int(r))
        pts = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def _draw_pill(self, tag: str, rect, text: str, kind: str = "ghost") -> None:
        t = self.tokens
        x1, y1, x2, y2 = rect
        fill = {"primary": t["accent"], "ghost": t["chip"], "danger": t["chip"],
                "active": t["accent_soft"]}[kind]
        fg = {"primary": "#FFFFFF", "ghost": t["chip_text"], "danger": t["err"],
              "active": t["accent"]}[kind]
        shape = self._round_rect(x1, y1, x2, y2, (y2 - y1) // 2, fill=fill, outline=fill)
        label = self.canvas.create_text(
            (x1 + x2) // 2, (y1 + y2) // 2, text=text, fill=fg, font=self.f_status
        )
        self.items[tag] = {"shape": shape, "label": label, "kind": kind, "fill": fill}
        self._bind_button(tag)

    def _bind_button(self, tag: str) -> None:
        canvas = self.canvas
        canvas.tag_bind(tag, "<Button-1>", lambda _e, name=tag: self._on_button(name))
        canvas.tag_bind(tag, "<Enter>", lambda _e, name=tag: self._set_button_hover(name, True))
        canvas.tag_bind(tag, "<Leave>", lambda _e, name=tag: self._set_button_hover(name, False))

    # -- 绘制 --------------------------------------------------------------
    def _redraw(self) -> None:
        c = self.canvas
        t = self.tokens
        self.rects = self._compute_layout()
        c.delete("all")
        self.items = {}
        r = self.rects
        x1, y1, x2, y2 = r["bubble"]
        tail_h = self.px(13)

        if self.frameless and self.use_tail:
            cx = self.tail_x
            if self.tail_edge == "top":
                c.create_polygon(cx - tail_h, y1 + self.px(3), cx, y1 - tail_h + self.px(1),
                                 cx + tail_h, y1 + self.px(3), fill=t["bubble"], outline="")
            else:
                c.create_polygon(cx - tail_h, y2 - self.px(3), cx, y2 + tail_h - self.px(1),
                                 cx + tail_h, y2 - self.px(3), fill=t["bubble"], outline="")
        self._round_rect(x1, y1, x2, y2, self.radius, fill=t["bubble"], outline=t["border"],
                         width=max(1, int(self.dpi)))
        if self.frameless and self.use_tail:
            cx = self.tail_x
            if self.tail_edge == "top":
                c.create_rectangle(cx - tail_h + self.px(4), y1 - 1, cx + tail_h - self.px(4),
                                   y1 + self.px(2), fill=t["bubble"], outline="")
            else:
                c.create_rectangle(cx - tail_h + self.px(4), y2 - self.px(2), cx + tail_h - self.px(4),
                                   y2 + 1, fill=t["bubble"], outline="")

        if self.state == "loading":
            track_y = y1 + max(2, self.px(2))
            left_x, right_x = x1 + self.radius, x2 - self.radius
            c.create_rectangle(left_x, track_y - 1, right_x, track_y + 1, fill=t["hairline"], outline="")
            span = right_x - left_x
            seg = int(span * 0.3)
            start = int(left_x + (span - seg) * self.progress)
            c.create_rectangle(start, track_y - 1, start + seg, track_y + 1, fill=t["accent"], outline="")

        ix1, iy1, ix2, iy2 = r["icon"]
        self._round_rect(ix1, iy1, ix2, iy2, self.px(8), fill=t["accent"], outline=t["accent"])
        c.create_text((ix1 + ix2) // 2, (iy1 + iy2) // 2 + self.px(1), text="译", fill="#FFFFFF",
                      font=self.f_icon)
        tx1, ty1, tx2, ty2 = r["title"]
        c.create_text(tx1, (ty1 + ty2) // 2, text="中英双语气泡", anchor="w", fill=t["text"],
                      font=self.f_title)
        hx1, hy1, hx2, hy2 = r["chip"]
        self._round_rect(hx1, hy1, hx2, hy2, (hy2 - hy1) // 2, fill=t["chip"], outline=t["chip"])
        c.create_text((hx1 + hx2) // 2, (hy1 + hy2) // 2, text=self.hotkey_text,
                      fill=t["chip_text"], font=self.f_chip)

        cxp = (r["close"][0] + r["close"][2]) // 2
        cyp = (r["close"][1] + r["close"][3]) // 2
        close_r = self.px(11)
        circle = c.create_oval(cxp - close_r, cyp - close_r, cxp + close_r, cyp + close_r,
                               fill="", outline="")
        cross = c.create_text(cxp, cyp, text="✕", fill=t["muted"], font=self.f_status)
        self.items["close"] = {"shape": circle, "label": cross, "kind": "icon", "fill": ""}
        self._bind_button("close")

        self._round_rect(*r["input_card"], self.px(12), fill=t["card"], outline=t["border"])
        hint = r["hint"]
        c.create_text(hint[2], (hint[1] + hint[3]) // 2, text="Ctrl+Enter 翻译", anchor="e",
                      fill=t["muted"], font=self.f_status)

        if self.has_result:
            for key, label, rail, color in (
                ("en", "ENGLISH", "en_rail", t["accent"]),
                ("zh", "回 译 中 文", "zh_rail", t["amber"]),
            ):
                self._round_rect(*r[f"{key}_card"], self.px(12), fill=t["card"], outline=t["border"])
                self._round_rect(*r[rail], self.px(2), fill=color, outline=color)
                lx1, ly1, _lx2, ly2 = r[f"{key}_label"]
                c.create_text(lx1, (ly1 + ly2) // 2, text=label, anchor="w", fill=t["muted"],
                              font=self.f_label)

        fx1, fy1, fx2, fy2 = r["footer"]
        cy = (fy1 + fy2) // 2
        dot_color = {"ok": t["ok"], "err": t["err"], "busy": t["accent"]}.get(self.status_kind, t["muted"])
        dot_r = max(3, self.px(4))
        self.items["status"] = [
            c.create_oval(fx1, cy - dot_r, fx1 + 2 * dot_r, cy + dot_r, fill=dot_color, outline=dot_color),
            c.create_text(fx1 + 2 * dot_r + self.px(7), cy, text=self._status_line(), anchor="w",
                          fill=t["err"] if self.status_kind == "err" else t["dim"], font=self.f_status),
        ]
        if self.copy_flash_until > time.time():
            chip_w = self.px(76)
            chip = (fx2 - chip_w, cy - self.px(11), fx2, cy + self.px(11))
            self._round_rect(*chip, self.px(11), fill=t["ok_bg"], outline=t["ok_bg"])
            self.items["copied"] = [
                c.create_text((chip[0] + chip[2]) // 2, cy, text="✓ 已复制", fill=t["ok"],
                              font=self.f_status)
            ]

        bx = fx1
        for tag, label, kind in (("translate", "翻译", "primary"), ("copy_en", "复制英文", "ghost"),
                                 ("copy_zh", "复制回译中文", "ghost"), ("copy_all", "复制全部", "ghost"),
                                 ("clear", "清空", "ghost")):
            width = self.px(24) + self.px(10.5) * len(label)
            self._draw_pill(tag, (bx, cy - self.px(11), bx + width, cy + self.px(11)), label, kind)
            bx += width + self.px(6)
        pin_label = "已固定" if self.pinned else "固定"
        pin_w = self.px(24) + self.px(10.5) * len(pin_label)
        self._draw_pill("pin", (fx2 - pin_w, cy - self.px(11), fx2, cy + self.px(11)), pin_label,
                        "active" if self.pinned else "ghost")
        self._update_footer_visibility()

    def _status_line(self) -> str:
        if self.state == "error":
            return "失败：" + (self.status_text or "未知错误")
        return self.status_text or ""

    def _update_footer_visibility(self) -> None:
        show_buttons = self.hover or self.pinned
        for key in ("translate", "copy_en", "copy_zh", "copy_all", "clear", "pin"):
            item = self.items.get(key)
            if item:
                state = "normal" if show_buttons else "hidden"
                self.canvas.itemconfigure(item["shape"], state=state)
                self.canvas.itemconfigure(item["label"], state=state)
        for key in ("status", "copied"):
            for item in self.items.get(key, []):
                self.canvas.itemconfigure(item, state="hidden" if show_buttons else "normal")

    def _place_widgets(self) -> None:
        r = self.rects
        t = self.tokens
        self._place(self.input, r["input_text"], t["card"], t["text"])
        if self.has_result:
            self._place(self.out_en, r["en_text"], t["card"], t["text"])
            self._place(self.out_zh, r["zh_text"], t["card"], t["dim"])
        else:
            self.out_en.place_forget()
            self.out_zh.place_forget()
        self._update_placeholder()

    def _place(self, widget, rect, bg, fg) -> None:
        x1, y1, x2, y2 = rect
        widget.configure(bg=bg, fg=fg, selectbackground=self.tokens["select"],
                         selectforeground=self.tokens["text"], insertbackground=self.tokens["accent"])
        widget.place(x=x1, y=y1, width=max(10, x2 - x1), height=max(10, y2 - y1))

    # -- 定时器与动效 ------------------------------------------------------
    def _schedule(self, delay_ms: int, func) -> None:
        holder = {}

        def wrapper():
            self._jobs.discard(holder.get("id"))
            func()

        holder["id"] = self.root.after(int(delay_ms), wrapper)
        self._jobs.add(holder["id"])

    def _cancel_jobs(self) -> None:
        for job in list(self._jobs):
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._jobs.clear()

    def _anim(self, duration_ms: int, apply, on_done=None, frames: int = 12) -> None:
        """缓出动画；animations=false 时直接落位。"""
        if not self.animations or duration_ms <= 0:
            apply(1.0)
            if on_done:
                on_done()
            return
        total = max(4, frames)
        step_ms = max(8, int(duration_ms / total))

        def tick(i: int) -> None:
            progress = min(1.0, i / float(total))
            apply(1.0 - (1.0 - progress) ** 3)
            if i < total:
                self._schedule(step_ms, lambda: tick(i + 1))
            elif on_done:
                on_done()

        tick(1)

    # -- 位置 --------------------------------------------------------------
    def _work_area(self) -> tuple:
        try:
            rect = wt.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _position_near_cursor(self) -> None:
        self.rects = self._compute_layout()
        left, top, right, bottom = self._work_area()
        cx = self.root.winfo_pointerx()
        cy = self.root.winfo_pointery()
        gap = self.px(10)
        height = self.rects["height"]
        if cy + gap + height <= bottom - self.px(8):
            self.tail_edge = "top"
            self.win_y = cy + gap
        else:
            self.tail_edge = "bottom"
            self.win_y = cy - gap - height
        self.rects = self._compute_layout()
        width = self.rects["width"]
        height = self.rects["height"]
        self.win_x = int(min(max(cx - width // 2, left + self.px(8)), right - width - self.px(8)))
        self.win_y = int(min(max(self.win_y, top + self.px(6)), bottom - height - self.px(6)))
        self.tail_x = int(min(max(cx - self.win_x, self.radius + self.px(18)),
                             width - self.radius - self.px(18)))

    def _apply_geometry(self, height: int) -> None:
        full = self.rects["height"]
        y = self.win_y + (full - height) if self.tail_edge == "bottom" else self.win_y
        self.root.geometry("%dx%d+%d+%d" % (self.rects["width"], int(height), int(self.win_x), int(y)))

    def _pointer_inside(self, margin: int = 0) -> bool:
        try:
            x, y = self.root.winfo_pointerxy()
            return (self.win_x - margin <= x <= self.win_x + self.rects["width"] + margin
                    and self.root.winfo_y() - margin <= y <= self.root.winfo_y() + self.root.winfo_height() + margin)
        except Exception:
            return False

    # -- 显示 / 隐藏 -------------------------------------------------------
    def show(self) -> None:
        self._hide_seq = getattr(self, "_hide_seq", 0) + 1
        self._cancel_jobs()
        self._refresh_theme()
        self._position_near_cursor()
        self._redraw()
        self._place_widgets()
        try:
            self.root.attributes("-topmost", bool(self.cfg.get("always_on_top", True)))
        except Exception:
            pass
        self.visible = True
        self.was_foreground = False
        self.last_fg_ours = False
        self.shown_at = time.time()
        self.root.deiconify()
        self.root.lift()
        self._force_foreground()
        self._schedule(70, self._force_foreground)
        self._schedule(180, self._force_foreground)
        self.logger.info("气泡显示 %dx%d+%d+%d tail=%s", self.rects["width"], self.rects["height"],
                         self.win_x, self.win_y, self.tail_edge)
        target = self.rects["height"]
        start = max(self.px(96), int(target * 0.6))

        def apply(progress: float) -> None:
            self._apply_geometry(int(start + (target - start) * progress))
            if self.translucency < 1.0:
                try:
                    self.root.attributes("-alpha", self.translucency * (0.4 + 0.6 * progress))
                except Exception:
                    pass

        self._anim(150, apply)
        self.input.focus_set()
        self._schedule(90, self._focus_input)
        self._start_tick()

    def _focus_input(self) -> None:
        try:
            self.input.focus_force()
        except Exception:
            pass

    def hide(self) -> None:
        if not self.visible:
            return
        self.logger.info("气泡隐藏 pinned=%s", self.pinned)
        self.visible = False
        self.pinned = False
        self._stop_tick()
        self._cancel_jobs()
        self._hide_seq = getattr(self, "_hide_seq", 0) + 1
        seq = self._hide_seq

        def finish() -> None:
            if seq == self._hide_seq and not self.visible:
                self.root.withdraw()
            self._cancel_jobs()

        if self.translucency < 1.0 and self.animations:
            def apply(progress: float) -> None:
                try:
                    self.root.attributes("-alpha", self.translucency * (1.0 - progress))
                except Exception:
                    pass

            self._anim(110, apply, on_done=finish, frames=8)
        else:
            self._schedule(1, finish)

    # -- 焦点与悬停 --------------------------------------------------------
    def _hwnd(self):
        try:
            return ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        except Exception:
            return self.root.winfo_id()

    def _force_foreground(self) -> None:
        """后台进程默认抢不到焦点，这里用 AttachThreadInput 推一把。"""
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        try:
            hwnd = self._hwnd()
            user32.ShowWindow(hwnd, 5)
            foreground = user32.GetForegroundWindow()
            if foreground != hwnd:
                tid_fg = user32.GetWindowThreadProcessId(foreground, None)
                tid_me = kernel32.GetCurrentThreadId()
                user32.AttachThreadInput(tid_fg, tid_me, True)
                user32.SetForegroundWindow(hwnd)
                user32.SetFocus(hwnd)
                user32.AttachThreadInput(tid_fg, tid_me, False)
            self.root.focus_force()
        except Exception:
            pass

    def _set_hover(self, value: bool) -> None:
        if self.hover == value:
            return
        self.hover = value
        self._update_footer_visibility()

    def _set_button_hover(self, tag: str, hovered: bool) -> None:
        item = self.items.get(tag)
        if not item:
            return
        if item.get("kind") == "icon":
            color = self.tokens["chip"] if hovered else ""
            self.canvas.itemconfigure(item["shape"], fill=color, outline=color)
            return
        if item["kind"] == "primary":
            color = self.tokens["accent_hover"] if hovered else self.tokens["accent"]
        elif item["kind"] == "active":
            color = self.tokens["select"] if hovered else self.tokens["accent_soft"]
        else:
            color = self.tokens["hover"] if hovered else self.tokens["chip"]
        self.canvas.itemconfigure(item["shape"], fill=color, outline=color)

    # -- 点外面收起 --------------------------------------------------------
    def _start_tick(self) -> None:
        self._stop_tick()
        self._tick_job = self.root.after(200, self._tick)

    def _stop_tick(self) -> None:
        job = getattr(self, "_tick_job", None)
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._tick_job = None

    def _tick(self) -> None:
        self._tick_job = None
        if not self.visible:
            return
        # 气泡的尖角指向鼠标，光标天然落在窗口外，所以用一小圈余量判定"还在用"。
        # 不用"前台窗口"做判定：无边框工具窗口抢不稳前台，会被浏览器之类的窗口抢回去。
        near = self._pointer_inside(self.px(48))
        if near != self.hover:
            self._set_hover(near)
        if (self.auto_dismiss and not self.pinned and not self.hover and not self.drag_offset
                and time.time() - self.shown_at > 1.5
                and not self._pointer_inside(self.px(160))):
            self.logger.info("自动收起：鼠标已移开")
            self.hide()
            return
        self._start_tick()

    # -- 状态切换 ---------------------------------------------------------
    def _apply_state(self, state: str, animate: bool = True) -> None:
        self.state = state
        old_height = 0
        try:
            old_height = self.root.winfo_height()
        except Exception:
            pass
        self._redraw()
        self._place_widgets()
        target = self.rects["height"]
        if animate and self.visible and old_height > self.px(80):
            start = old_height

            def apply(progress: float) -> None:
                self._apply_geometry(int(start + (target - start) * progress))

            self._anim(160, apply)
        else:
            self._apply_geometry(target)

    def set_status(self, text: str, kind: str = "info") -> None:
        self.status_text = text
        if kind != "info":
            self.status_kind = kind
        elif self.status_kind not in ("busy",):
            self.status_kind = "idle"

    # -- 事件循环 ---------------------------------------------------------
    def poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "fire":
                    self.show()
                elif kind == "status":
                    self.set_status(payload, "busy")
                    if self.visible:
                        self._redraw()
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
            text = f"热键已接管：{detail}"
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
        self.set_status(text, "err" if mode in ("failed", "invalid") else "idle")
        if self.visible:
            self._redraw()
        if mode in ("fallback", "failed", "invalid"):
            self.show()
            self.messagebox.showwarning("中英双语气泡", text)

    # -- 交互 -------------------------------------------------------------
    def _on_ctrl_enter(self, _event):
        self.start_translate()
        return "break"

    def start_translate(self) -> None:
        if self.busy:
            return
        text = self.input.get("1.0", "end").strip()
        if not text:
            self.set_status("先输入要翻译的内容", "err")
            self._redraw()
            return
        self.busy = True
        self.has_result = False
        self.set_text(self.out_en, "")
        self.set_text(self.out_zh, "")
        self.status_text = "正在翻译…"
        self.status_kind = "busy"
        self.progress = 0.0
        self._apply_state("loading")
        self._animate_progress()
        threading.Thread(target=self._worker, args=(text,), daemon=True, name="translate").start()

    def _animate_progress(self) -> None:
        if not self.visible or self.state != "loading":
            return
        self.progress = (self.progress + 0.06) % 1.0
        self._redraw()
        self._schedule(45, self._animate_progress)

    def _worker(self, text: str) -> None:
        try:
            result = translate(text, self.cfg, self.logger,
                               progress=lambda msg: self.events.put(("status", msg)))
            self.events.put(("done", result))
        except TranslationError as exc:
            self.logger.warning("翻译失败：%s", exc)
            self.events.put(("error", str(exc)))
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("翻译异常")
            self.events.put(("error", f"{type(exc).__name__}: {exc}"))

    def on_done(self, result: dict) -> None:
        self.busy = False
        self.set_text(self.out_en, result["english"])
        self.set_text(self.out_zh, result["chinese"])
        self.has_result = True
        if result["direction"] == "zh2en":
            direction_text = "中文 → 英文 + 独立回译"
            primary = result["english"]
        else:
            direction_text = "英文 → 中文"
            primary = result["chinese"]
        self.status_text = f"{direction_text} · 原文 {result['chars']} 字符 · {result['elapsed']:.1f}s"
        self.status_kind = "ok"
        self._apply_state("result")
        if self.cfg.get("auto_copy_english", True) and primary:
            self.copy_to_clipboard(primary)
            self._flash_copied()

    def on_error(self, message: str) -> None:
        self.busy = False
        self.status_text = message
        self.status_kind = "err"
        self._apply_state("error")

    def _flash_copied(self) -> None:
        self.copy_flash_until = time.time() + 1.5
        self._redraw()
        self._schedule(1600, self._clear_copied)

    def _clear_copied(self) -> None:
        self.copy_flash_until = 0.0
        if self.visible:
            self._redraw()

    def _on_button(self, name: str) -> None:
        if name == "translate":
            self.start_translate()
        elif name == "copy_en":
            self.copy_field("english")
        elif name == "copy_zh":
            self.copy_field("chinese")
        elif name == "copy_all":
            self.copy_all()
        elif name == "clear":
            self.clear_all()
        elif name == "pin":
            self.toggle_pin()
        elif name == "close":
            self.hide()

    def toggle_pin(self) -> None:
        self.pinned = not self.pinned
        self._redraw()

    # -- 拖动 -------------------------------------------------------------
    def _on_canvas_press(self, event) -> None:
        try:
            self.drag_offset = (event.x_root - self.win_x, event.y_root - self.root.winfo_y())
        except Exception:
            self.drag_offset = None

    def _on_canvas_drag(self, event) -> None:
        if not self.drag_offset:
            return
        dx, dy = self.drag_offset
        self.win_x = int(event.x_root - dx)
        self.win_y = int(event.y_root - dy)
        self.root.geometry("+%d+%d" % (self.win_x, self.win_y))

    def _on_canvas_release(self, _event) -> None:
        self.drag_offset = None

    # -- 滚轮 -------------------------------------------------------------
    def _on_wheel(self, event):
        x = event.x_root - self.win_x
        y = event.y_root - self.root.winfo_y()
        target = None
        for key, widget in (("input_text", self.input), ("en_text", self.out_en), ("zh_text", self.out_zh)):
            rect = self.rects.get(key)
            if rect and rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]:
                target = widget
                break
        if target is None:
            return None
        target.yview_scroll(-3 if event.delta > 0 else 3, "units")
        return "break"

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
            self.set_status("这块还是空的", "err")
            self._redraw()
            return
        self.copy_to_clipboard(text)
        self.status_text = "已复制" + ("英文" if field == "english" else "回译中文")
        self.status_kind = "ok"
        self._flash_copied()

    def copy_all(self) -> None:
        english = self.get_text(self.out_en)
        chinese = self.get_text(self.out_zh)
        if not english and not chinese:
            self.set_status("还没有结果", "err")
            self._redraw()
            return
        parts = []
        if english:
            parts.append("English:\n" + english)
        if chinese:
            parts.append("回译中文:\n" + chinese)
        self.copy_to_clipboard("\n\n".join(parts))
        self.status_text = "已复制全部"
        self.status_kind = "ok"
        self._flash_copied()

    def clear_all(self) -> None:
        self.input.delete("1.0", "end")
        self.set_text(self.out_en, "")
        self.set_text(self.out_zh, "")
        self.has_result = False
        self.status_text = "输入中文开始翻译"
        self.status_kind = "idle"
        self._apply_state("input")
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


# --------------------------------------------------------------------------
# 界面快照（开发验收用：python translate_popup.py --snapshot）
# --------------------------------------------------------------------------

SNAPSHOT_ZH = ("就是那个，帮我写个脚本，把文件夹里所有 png 批量转成 webp，emmm 质量别太低啊，"
               "大概 85 左右就行，还有记得保留原文件，别给我删了，你懂我意思吧")
SNAPSHOT_EN = ("Um, that one, help me write a script, batch convert all the png in the folder to "
               "webp, emmm don't make the quality too low, around 85 is fine, and also remember to "
               "keep the original files, don't delete them for me, you know what I mean right")
SNAPSHOT_BACK = ("嗯，那个，帮我写个脚本，把文件夹里所有的png批量转成webp，emmm质量别弄太低，"
                 "85左右就行，还有记得保留原文件，别给我删了，你懂我意思吧")


def _grab_png(x: int, y: int, w: int, h: int, path: str) -> None:
    """从屏幕抓一块区域存成 PNG（只用标准库）。"""
    import struct
    import zlib

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi32.CreateDIBSection.restype = ctypes.c_void_p
    gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                       ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_uint]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint]

    class BMIH(ctypes.Structure):
        _fields_ = [
            ("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
            ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
            ("biSizeImage", wt.DWORD), ("biXPels", ctypes.c_long), ("biYPels", ctypes.c_long),
            ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
        ]

    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    header = BMIH()
    header.biSize = ctypes.sizeof(BMIH)
    header.biWidth = w
    header.biHeight = h
    header.biPlanes = 1
    header.biBitCount = 24
    header.biCompression = 0
    bits = ctypes.c_void_p()
    bitmap = gdi32.CreateDIBSection(mem, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(mem, bitmap)
    gdi32.BitBlt(ctypes.c_void_p(mem), 0, 0, w, h, ctypes.c_void_p(screen), x, y, 0x00CC0020)
    stride = ((w * 3 + 3) // 4) * 4
    raw = ctypes.string_at(bits, stride * h)
    gdi32.SelectObject(mem, old)
    gdi32.DeleteObject(ctypes.c_void_p(bitmap))
    gdi32.DeleteDC(ctypes.c_void_p(mem))
    user32.ReleaseDC(None, screen)

    rows = bytearray()
    for row in range(h - 1, -1, -1):
        line = bytearray(raw[row * stride:row * stride + w * 3])
        line[0::3], line[2::3] = line[2::3], line[0::3]
        rows.append(0)
        rows.extend(line)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(rows), 6))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(png)


def run_snapshot(logger: logging.Logger) -> int:
    import tkinter as tk

    enable_dpi_awareness()
    out_dir = os.path.join(APP_DIR, "snapshots")
    os.makedirs(out_dir, exist_ok=True)
    cases = (
        ("empty", "light", "input", "idle", "输入中文开始翻译"),
        ("input", "light", "input", "idle", "输入中文开始翻译"),
        ("loading", "light", "loading", "busy", "正在做独立的回译中文（只看英文）…"),
        ("result", "light", "result", "ok", "中文 → 英文 + 独立回译 · 原文 79 字符 · 2.0s"),
        ("hover", "light", "result", "ok", "中文 → 英文 + 独立回译 · 原文 79 字符 · 2.0s"),
        ("error", "light", "error", "err", "接口返回 HTTP 401：Authentication Fails"),
        ("dark_input", "dark", "input", "idle", "输入中文开始翻译"),
        ("dark_result", "dark", "result", "ok", "中文 → 英文 + 独立回译 · 原文 79 字符 · 2.0s"),
        ("dark_hover", "dark", "result", "ok", "中文 → 英文 + 独立回译 · 原文 79 字符 · 2.0s"),
    )
    for name, theme, state, kind, status in cases:
        cfg = load_config()
        cfg["frameless"] = True
        cfg["animations"] = False
        cfg["theme"] = theme
        root = tk.Tk()
        app = PopupApp(root, cfg, logger)
        if name != "empty":
            app.input.insert("1.0", SNAPSHOT_ZH)
        app.set_text(app.out_en, SNAPSHOT_EN)
        app.set_text(app.out_zh, SNAPSHOT_BACK)
        app.has_result = state in ("result", "error")
        app.state = state
        app.status_kind = kind
        app.status_text = status
        app.hover = name.endswith("hover")
        app.pinned = False
        app.progress = 0.35
        app.tail_edge = "top"
        app.win_x = 0
        app.win_y = 0
        app._refresh_theme()
        app.rects = app._compute_layout()
        app.tail_x = int(app.rects["width"] * 0.26)
        app.visible = True
        app._redraw()
        app._place_widgets()
        # 快照要求完全不透明：否则会和桌面混色，看不出真实效果
        try:
            root.attributes("-alpha", 1.0)
        except Exception:
            pass
        try:
            root.attributes("-transparentcolor", "")
        except Exception:
            pass
        app.canvas.configure(bg="#E9ECF1" if theme == "light" else "#101215")
        app._apply_geometry(app.rects["height"])
        root.deiconify()
        root.lift()
        root.update_idletasks()
        root.update()
        time.sleep(0.6)
        root.update()
        path = os.path.join(out_dir, f"{name}.png")
        _grab_png(0, 0, app.rects["width"], app.rects["height"], path)
        print(f"snapshot {name}: {app.rects['width']}x{app.rects['height']} -> {path}")
        root.destroy()
    return 0


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
    verbose = any(a in args for a in ("--once", "--selftest", "--install-startup", "--uninstall-startup",
                                      "--probe", "--snapshot"))
    logger = setup_logging(verbose_console=verbose)
    try:
        if "--selftest" in args:
            return run_selftest()
        if "--once" in args:
            return run_once(logger)
        if "--snapshot" in args:
            return run_snapshot(logger)
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
