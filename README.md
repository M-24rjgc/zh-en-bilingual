<div align="center">

# 中英双语气泡

**按下 Copilot 键，中文提示词立刻变成地道英文——再用一次"看不见原文"的回译，让你亲眼确认它没翻译跑偏。**

![platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6?style=flat-square)
![python](https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![dependencies](https://img.shields.io/badge/dependencies-none-brightgreen?style=flat-square)
![hotkey](https://img.shields.io/badge/hotkey-Copilot%20key-8A2BE2?style=flat-square)
![stdlib](https://img.shields.io/badge/pure-Python%20stdlib-F7DF1E?style=flat-square&logo=python&logoColor=black)

<img src="assets/demo.png" width="620" alt="按下热键后的中英双语气泡：中文输入、英文译文、回译中文">

</div>

---

## 你大概遇到过这些事

把中文提示词丢给 AI 之前，得先翻译成英文；翻完又心里发虚：
**"它真的把我想说的翻出来了吗？"**

于是你复制回来、粘进翻译软件、再翻回中文看一眼——一来一回，半分钟没了。

这个工具把这一整套动作压成**一个按键**：弹出输入框 → 粘贴中文 → `Ctrl+Enter` → 英文直接进剪贴板，回译中文就在下面等着你验收。

## 它和"随便找个翻译"有什么不一样

| | 普通翻译工具 | 中英双语气泡 |
| --- | --- | --- |
| 回译怎么来的 | 看不到，或者只是把原文换句话 | **独立一次请求，只能看到英文**，所以能真的用来验证 |
| 你的语气词 | 被"优化"掉 | `emmm`、`就是那个`、`你懂我意思吧` 原样保留 |
| 代码 / JSON / 变量名 | 顺手给你翻译了 | 原样不动，只翻自然语言 |
| 触发方式 | 切窗口、复制、粘贴 | 任意软件里**按一个键** |
| 依赖 | 装软件、登录、订阅 | Windows + Python，**零第三方库** |

## 它不是"优化"你的提示词

这是设计前提：**它只翻译，不改造。**

提示词里明确禁止润色、精简、重排、"顺手帮你写得更清楚"，也禁止补充解释。你写 `大概 85 左右就行`，它就给 `around 85 is fine`，不会贴心地换成 `optimal quality setting of 85`。

真实运行结果（就是上面那张截图）：

```
中文原文
就是那个，帮我写个脚本，把文件夹里所有 png 批量转成 webp，emmm 质量别太低啊，
大概 85 左右就行，还有记得保留原文件，别给我删了，你懂我意思吧

English
Um, that one, help me write a script, batch convert all the png in the folder to webp,
emmm don't make the quality too low, around 85 is fine, and also remember to keep the
original files, don't delete them for me, you know what I mean right

回译中文
嗯，那个，帮我写个脚本，把文件夹里所有的png批量转成webp，emmm质量别弄太低，
85左右就行，还有记得保留原文件，别给我删了，你懂我意思吧
```

中文进 → 双语出；**英文进 → 只出中文**。方向自动判断，你不用切模式。

## 30 秒上手

```powershell
git clone https://github.com/M-24rjgc/zh-en-bilingual.git
cd zh-en-bilingual

python translate_popup.py --install-startup   # 开机常驻，无黑窗
python translate_popup.py                     # 或者现在就手动跑起来
```

然后**按 Copilot 键**，就能用了。

第一次用之前，在 `config.json` 里填好你的接口（默认 DeepSeek；也可以留空，直接读取本机 `~/.codex/config.toml` 里的 token）：

```json
{
  "model": "deepseek-flash",
  "base_url": "https://api.deepseek.com",
  "api_key": ""
}
```

> 任何兼容 OpenAI `chat/completions` 的接口都能用。接口越快，体验越接近"按键即出"。

## 操作

| 按键 / 动作 | 效果 |
| --- | --- |
| `Copilot 键` | 在鼠标附近弹出输入框（任意软件里都能按） |
| `Ctrl+Enter` | 翻译 |
| `Esc` | 关掉窗口，后台继续待命 |
| 翻译完成 | 主要译文（英文输入时是中文）**自动进剪贴板**，`Ctrl+V` 就能贴进 ChatGPT |

窗口上还有「复制英文 / 复制回译中文 / 复制全部 / 清空」。

实测速度：中译英 + 回译约 **2–3 秒**，英译中约 **1 秒**（deepseek-flash，关闭推理）。

## 回译为什么可信

```
你的中文 ──① 中译英──▶ 英文译文 ──▶ 剪贴板 / 提示词
              │
              └──② 全新请求（只能看到英文）──▶ 回译中文
```

第二次请求是一个**全新的、干净的会话**，里面只有那份英文。它不知道你原本写了什么，所以你拿到的回译是你原文的"镜像"，而不是模型照着原文的复述。

## 常用的几条命令

```powershell
python translate_popup.py                 # 后台常驻
python translate_popup.py --probe         # 按键探测：按一下你的 Copilot 键，看它发什么键码
python translate_popup.py --install-startup
python translate_popup.py --uninstall-startup
python translate_popup.py --once          # 从 stdin 读文本，直接吐 JSON（适合接进脚本）
python translate_popup.py --selftest      # 离线自测语言判定
```

用 `pythonw.exe` 启动可以完全没有控制台窗口（`--install-startup` 已经帮你这么配了）。

## Copilot 键是怎么被"接管"的

Windows 11 上 Copilot 键发的是 `Win+Shift+F23`。多数情况下这个组合已经被系统占用，于是本工具退到**低级键盘钩子**，把按键直接吞掉——Copilot 不再弹出来，按键归你。

想换成别的键：`python translate_popup.py --probe` → 按一下你想要的键 → 点「用这个组合键作为热键并保存」→ 重启程序。

## 配置项

| 字段 | 说明 |
| --- | --- |
| `hotkey` / `fallback_hotkey` | 主热键与备用热键，如 `win+shift+f23`、`ctrl+alt+z` |
| `model` / `base_url` / `api_key` | 接口信息；`api_key` 留空则从 `~/.codex/config.toml` 读取 |
| `codex_config_path` | 上面那个文件的路径；留空 = `~/.codex/config.toml` |
| `reasoning_effort` | `none` 最省时（每次约 1–2 秒）；留空表示不带这个参数 |
| `max_tokens` / `timeout_sec` | 单次请求预算与超时 |
| `auto_copy_english` / `always_on_top` / `font_size` | 界面行为 |

## 隐私

- API token 只在运行时从本机 `~/.codex/config.toml` 读取，**不会**被复制进这个项目。
- 除了你自己配置的接口，不向任何地方发送内容。
- `logs/translate.log` 只记录方向、字符数、耗时和错误，**不记录文本内容**。

## 已知限制

- 目前只支持 Windows（依赖 `RegisterHotKey` / 低级键盘钩子）。
- 需要你自己准备一个兼容 OpenAI 的接口。
- 需要 Python 3.10+；本工具只用标准库，不装任何包。
- 仓库暂未附加开源许可证，需要的话告诉我，我可以补 MIT。

---

## English

**A Windows popup translator on a global hotkey: Chinese in, English plus an independent back-translation out.**

Paste a Chinese prompt, hit `Ctrl+Enter`, and the English lands in your clipboard while a *separately generated* Chinese back-translation appears right below it — so you can check the translation yourself. Paste English and you get Chinese only. Tone, filler words and hesitations (`emmm`, `你懂我意思吧`) are preserved on purpose: the prompts explicitly forbid polishing, optimizing or restructuring, and code blocks, JSON, URLs and identifiers are left untouched.

Two independent API calls make the back-translation an honest check — the second request only ever sees the English.

Pure Python standard library (no `pip install`, no AutoHotkey), works with any OpenAI-compatible `chat/completions` endpoint, and takes over the Copilot key so it no longer opens Copilot.
