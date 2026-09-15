# zh-en-bilingual

A tiny Windows popup translator driven by a global hotkey: type or paste Chinese and it gives
you the English translation **plus an independently produced back-translation into Chinese**, so
you can check the translation yourself. Paste English and it just gives you Chinese.

Tone, filler words and hesitations (`就是那个`, `emmm`, `你懂我意思吧`) are preserved on purpose —
the prompts explicitly forbid polishing, optimizing or restructuring the text, and code blocks,
JSON, URLs and identifiers are left untouched.

Windows 专用的小工具：按下全局热键弹出输入框，输入中文就同时给你「英文译文 + 独立回译中文」
（回译那一路看不到原文，所以能真的用来校验翻译），输入英文就只给中文。提示词明确要求保留语气词、
不做润色优化，代码块 / JSON / 变量名 / URL 原样保留。

## Requirements

- Windows 10 / 11
- Python 3.10+ (standard library only — no `pip install`, no AutoHotkey)
- An OpenAI-compatible chat completions endpoint (default: DeepSeek)

## Run

```powershell
python translate_popup.py               # resident in background, waits for the hotkey
python translate_popup.py --probe       # key probe: press a key to see what code it sends
python translate_popup.py --install-startup
python translate_popup.py --uninstall-startup
python translate_popup.py --once        # read text from stdin, print JSON (no GUI)
python translate_popup.py --selftest    # language-detection self test, offline
```

Use `pythonw.exe` for a windowless background process (the `--install-startup` shortcut does this
for you). Start it with `python.exe` if you want to watch console output.

## Hotkey

`config.json` ships with `win+shift+f23`, which is what the Copilot key sends on Windows 11.
If another process already owns that combination (a `RegisterHotKey` error 1409), the tool falls
back to a low-level keyboard hook and swallows the key anyway, so Copilot no longer opens. If both
fail it falls back to `ctrl+alt+z`.

To bind a different key: run `--probe`, press the key you want, then click
"用这个组合键作为热键并保存" and restart the app.

## Keys

- `Ctrl+Enter` translate, `Esc` close
- Chinese input -> `English` + `回译中文`; English input -> `中文译文` only
- The primary output is copied to the clipboard automatically

## Configuration

`config.json`:

| field | meaning |
| --- | --- |
| `hotkey` / `fallback_hotkey` | global hotkeys, e.g. `win+shift+f23`, `ctrl+alt+z` |
| `model` / `base_url` / `api_key` | chat completions endpoint; leave `api_key` empty to read it from `~/.codex/config.toml` |
| `codex_config_path` | where to read that token from; empty = `~/.codex/config.toml` |
| `reasoning_effort` | `none` keeps latency around 1-2s per call; empty string disables the parameter |
| `max_tokens`, `timeout_sec` | per-request budget and timeout |
| `auto_copy_english`, `always_on_top`, `font_size` | UI behaviour |

## Privacy

The API token is read from `~/.codex/config.toml` at runtime and is never copied into this
project. Nothing is sent anywhere except your configured endpoint. `logs/translate.log` stores
direction, character counts, timings and errors — not the text itself.

## Translation pipeline

Chinese input performs **two independent requests**: `zh -> en`, then `en -> zh` where the second
request can only see the English. That is what makes the back-translation a real check instead of
the model paraphrasing your original.
