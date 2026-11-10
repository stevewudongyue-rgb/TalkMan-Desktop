# TALKMAN Desktop

**把话说出来，改好再发。**

TALKMAN 是 Windows 和 Mac 上的语音预输入工具：说话生成文字，用自然语言修改，检查后填入其他应用的输入框。本仓库公开桌面端完整源码与安装包，采用 [Apache License 2.0](LICENSE)。所有随版本提供的功能均直接开放，无需兑换码或账号激活。

## 下载 · 0.1.46（构建 47）

| 电脑 | 下载与运行 |
|---|---|
| Windows 10／11，64 位 | **[下载 Windows 便携版](https://github.com/stevewudongyue-rgb/TalkMan-Desktop/releases/download/v0.1.46/TalkMan-Windows-Portable-0.1.46.zip)**。完整解压后运行 `TalkMan/TalkMan.exe`，保留相邻的 `_internal` 文件夹。 |
| Apple 芯片 Mac | 本轮 0.1.46 安装包待公司 M2 原生编译验证；可按下方说明自行构建。 |

[本次发布与附件](https://github.com/stevewudongyue-rgb/TalkMan-Desktop/releases/tag/v0.1.46) · [全部版本](https://github.com/stevewudongyue-rgb/TalkMan-Desktop/releases) · [版本记录](TalkMan-版本记录.md)

分享下载直接发送本页地址：**https://github.com/stevewudongyue-rgb/TalkMan-Desktop**。下载无需 GitHub 账号。`Code → Download ZIP` 和发布页的 `Source code` 是源码；运行程序请下载上表中的安装包。

## 能做什么

- **录音和覆盖：** 没有选区就在光标处输入，有选区就替换所选文字。支持点按启停、按住录音和自定义快捷键。
- **语音指令：** 说“把这句改得简短一些”“把这一段移到最后”，也可保存草稿、切换 AI 设置或记住约定词。需要确认时，在原窗口继续选择或回答。
- **AI 整理与翻译：** 去语气词、补标点、分行、理顺语序；可先看改写对照，确认后应用。翻译只处理选中文字。
- **草稿、历史和约定词：** 保存文本、查看投递记录、导出 TXT；通过「多选 → 勾选／全选 → 删除 → 确认」批量清理。记住专名及容易听错的词。
- **填入 Agent：** 选择目标窗口后，把文字放进它的输入框。确认填入成功后，再点“下发”才发送；无法核对时保留正文。支持窗口置顶、边缘收起和快捷唤起。

详细操作及自然说法见 [语音指令清单](TalkMan-语音指令清单.md)。不同应用的输入框兼容性可能不同。

## 第一次使用

1. 在设置中连接**语音识别模型**与**文字改写模型**。支持千问及其他已接入服务；需要自己的服务凭证。
2. 点击“录音和覆盖”说话，停止后检查文字；选中文字后可用“指令修改”。
3. 选择目标应用，点击“填入 Agent”；核对后再次点击“下发”。

软件本身免费，模型服务的费用与额度由服务商决定。已有免费在线内测连接可继续使用，但受服务方策略、额度和网络影响，可能暂停或不可用；公开安装包不附带供应商 Key 或免费内测凭证。

录音需要麦克风权限；Mac 填入其他应用需要辅助功能权限。当前分发包尚未商业签名／公证，系统可能提示确认来源。Mac 红色关闭按钮关闭窗口，`⌘Q` 才退出应用。

## 在线升级

打开 **设置 → 版本 → 检查更新**，下载后选择“保存并重启升级”。正文、草稿、已记住的 Key、快捷键和外观保留。启动检查只提醒，不自动安装。0.1.32 及以前版本需要先手动安装一次新版。

## 从源码运行与构建

安装含 Tcl/Tk 的 Python 3.12 或 3.13。在本仓库根目录运行：

```sh
python -m pip install -r TalkMan-requirements.txt
python talkman_app.py
```

Mac 使用 `python3`。源码运行仍需自己的模型凭证和系统权限。

**Windows 便携包：**

```powershell
python -m pip install --target TalkMan-libs -r TalkMan-requirements.txt pyinstaller==6.22.3
powershell -ExecutionPolicy Bypass -File .\build_windows.ps1
```

输出在 `dist-desktop/TalkMan/`，请分发整个文件夹。构建脚本会收集运行依赖、图标和第三方许可。

**Apple 芯片 Mac：** 在 Mac 上安装 python.org 的 Python，再运行：

```sh
bash TalkMan-构建Mac.command
```

脚本准备依赖并构建应用和 DMG。目标最低系统为 macOS 14；各系统的实际兼容性需要在对应设备验收。公司 M2 优先构建，GitHub Actions 不会自动启动编译。正式分发的签名、公证需维护者自备 Apple Developer ID。

**验证：**

```sh
python -m unittest test_talkman_license test_talkman_updates test_talkman_update_ui -q
python TalkMan-构建Mac.py --check
```

涉及界面的测试需要桌面图形环境。完整测试见 `test_talkman_*.py`；测试使用临时数据和模拟模型，不代表真实麦克风及所有目标应用均已验收。

## 许可与反馈

源码及构建产物采用 **Apache License 2.0**，允许使用、修改、商用与再分发，并须遵守版权及许可保留要求。商标与 Logo 不随代码许可授予；字体、图标和依赖遵守各自许可。本仓库的开源范围仅为桌面端。

[许可证全文](LICENSE) · [软件说明](TalkMan-软件使用协议.md) · [隐私说明](TalkMan-隐私说明.md) · [第三方组件](TalkMan-第三方组件说明.md)

版权所有：杭州行至云起科技有限公司；项目负责人：吴冬悦。

问题请提交到 [Issues](https://github.com/stevewudongyue-rgb/TalkMan-Desktop/issues)，附系统版本、软件版本与操作步骤。不要公开 API Key、密码、录音或私密正文。
