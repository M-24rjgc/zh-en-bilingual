# 中英双语气泡

一个 Windows 小工具：按一下 Copilot 键，把中文提示词翻成英文，再独立地翻回中文，方便你对照检查。英文会自动放进剪贴板。

A Windows hotkey tool that turns Chinese prompts into English, plus an independent back-translation so you can check the result yourself.

![Windows](https://img.shields.io/badge/Windows-10%2F11-0078D6?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-3DA639?style=flat-square)

![demo](assets/demo.png)

## 它做什么

输入中文，会得到两份结果：一份英文，一份把英文再翻回来的中文。回译那一步只看英文、看不到你的原文，所以拿它跟原文一对照，翻译有没有跑偏就很直观。输入英文的话，就只给一份中文。

「屏幕辅助」是可选功能，默认关闭。手动开启后，每次在气泡中提交翻译时，截取唤起气泡前所用应用所在屏幕的当前画面，随原文发送到配置的翻译接口，帮助判断术语和指代。截图排除翻译气泡，最长边默认缩至 1920 像素；只在内存中处理，不保存图片文件。回译仍只看英文，不附截图。点击气泡顶部「屏幕辅助：开/关」可切换，设置会保存。截图失败时继续纯文字翻译，并在状态栏提示。

「思考」也是独立的可选功能，默认关闭。点击顶部「思考：关」后，使用配置中的思考强度（默认 `low`）；再次点击关闭。两个开关互不影响。

翻译刻意不做润色。`emmm`、`就是那个`、`你懂我意思吧` 这类语气会原样留下，代码块、JSON、变量名、URL 也保持原样，只翻周围的话。下面是一次真实运行：

```text
就是那个，帮我写个脚本，把文件夹里所有 png 批量转成 webp，emmm 质量别太低啊，
大概 85 左右就行，还有记得保留原文件，别给我删了，你懂我意思吧

Um, that one, help me write a script, batch convert all the png in the folder to webp,
emmm don't make the quality too low, around 85 is fine, and also remember to keep the
original files, don't delete them for me, you know what I mean right

嗯，那个，帮我写个脚本，把文件夹里所有的png批量转成webp，emmm质量别弄太低，
85左右就行，还有记得保留原文件，别给我删了，你懂我意思吧
```

界面是一块无边框的圆角气泡，尖角指向鼠标。触发时只是个小输入条，出结果后往下展开，配色跟着系统深浅色走，底部按钮一直都在。

![dark](assets/dark.png)

## 安装和使用

```powershell
git clone https://github.com/M-24rjgc/zh-en-bilingual.git
cd zh-en-bilingual
python translate_popup.py --install-startup   # 开机常驻；也可以直接 python translate_popup.py
```

之后按 Copilot 键就行。

开机常驻由 Windows 任务计划程序管理，登录后启动，每分钟检查恢复。安装任务可能需要管理员确认，翻译程序始终以当前用户的普通权限运行。后台守护进程负责恢复翻译进程；连续快速失败时降低重试频率。键盘钩子每 15 秒重新挂接，热键线程停止响应时重启翻译进程。

- `Enter` 直接翻译，`Shift+Enter` 换行，`Esc` 关闭
- `Ctrl+1` / `Ctrl+2` 复制英文 / 复制回译中文，`Ctrl+Shift+C` 复制全部
- 鼠标移开气泡会自动收起，想让它留着就点「固定」
- 按住气泡空白处可以拖动

## 配置

都在 `config.json` 里。默认模型是 `deepseek-flash`，接口地址 `https://api.deepseek.com`。在 `api_key` 填入 DeepSeek 官方 API key；留空时读取环境变量 `DEEPSEEK_API_KEY`，也可以用 `api_key_env` 指定其他环境变量名。每次翻译会重新读取配置文件；修改环境变量后需要重启程序。

其余常用项：`hotkey`（默认 `win+shift+f23`，也就是 Copilot 键）、`theme`（`auto` / `light` / `dark`）、`accent`（强调色，留空取系统色）、`translucency`（`1.0` 为完全不透明）、`frameless`（输入法候选框异常时改成 `false` 退回标题栏）。

`screen_context` 控制气泡的屏幕辅助，默认 `false`；`screenshot_max_edge` 控制截图最长边（640–2560 像素）。开启屏幕辅助需要支持图片输入的模型，默认的 DeepSeek 官方 `deepseek-flash` 支持。排除气泡需要 Windows 10 2004 或更新版本。命令行 `--once` 只发送输入文字。

`thinking_enabled` 控制是否思考，默认 `false`；开启后才使用 `reasoning_effort` 的强度，关闭时请求明确使用 `none`。开关只在手动点击时保存对应字段，保留其他设置。

## 其他命令

```powershell
python translate_popup.py --probe          # 按一个键，看它实际发什么键码
python translate_popup.py --once           # 从 stdin 读文本，输出 JSON
python translate_popup.py --selftest       # 离线自测语言判定
python translate_popup.py --snapshot       # 渲染各状态截图（开发用）
python translate_popup.py --uninstall-startup
```

## 说明

- 只支持 Windows，纯 Python 标准库，不用装任何东西。
- 需要一个兼容 OpenAI `chat/completions` 的接口。
- Copilot 键发的是 `Win+Shift+F23`；如果这个组合被系统占用，程序会退到低级键盘钩子把它接管，Copilot 不再弹。
- `logs/translate.log` 只记方向、字符数、耗时和错误，不记翻译内容。

## License

MIT，见 [LICENSE](LICENSE)。
