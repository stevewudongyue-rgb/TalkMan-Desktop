# Talk Man Desktop

把话说出来，改好再发。

Talk Man 是桌面上的预输入工具：录音变成文字，用语音告诉 AI 怎么修改，确认后再放进其他应用的输入框。

## 直接下载 · 0.1.34 测试版

**[早期体验：领取 10 枚一年高级功能兑换码](https://github.com/stevewudongyue-rgb/TalkMan-Desktop/releases/tag/v0.1.32-test)**。兑换码位于发布说明顶部，每码限 1 台设备，先到先得；授权从 2026-11-03 发放时起算，至 2027-11-03 到期，不包含模型调用额度。

| 电脑 | 点击下载 | 下载后怎么打开 |
|---|---|---|
| Windows 10／11，64 位 | **[下载 Windows 版（ZIP，约 33 MB）](https://github.com/stevewudongyue-rgb/TalkMan-Desktop/releases/download/v0.1.34-test/TalkMan-Windows-Portable-0.1.34.zip)** | 完整解压，打开文件夹里的 `TalkMan.exe`；保留 `_internal` 文件夹。 |
| Apple 芯片 Mac，macOS 14 及以上 | **[下载 Mac 版（DMG，约 21 MB）](https://github.com/stevewudongyue-rgb/TalkMan-Desktop/releases/download/v0.1.34-test/TalkMan-Mac-AppleSilicon-0.1.34-test.dmg)** | 打开 DMG，将 TalkMan 拖到“应用程序”，再从那里运行。 |

下载不需要 GitHub 账号。**分享给朋友时，直接发送本页地址：<https://github.com/stevewudongyue-rgb/TalkMan-Desktop>**，也可以复制上方对应电脑的下载链接。

[查看版本说明和下载附件](https://github.com/stevewudongyue-rgb/TalkMan-Desktop/releases/tag/v0.1.34-test) · [全部版本](https://github.com/stevewudongyue-rgb/TalkMan-Desktop/releases)

在版本页面往下找到 **Assets（附件）**，点击以 `TalkMan-Windows` 或 `TalkMan-Mac` 开头的文件。GitHub 的 `Code → Download ZIP` 和 `Source code` 下载项只包含本仓库的说明文件，不是应用安装包。

本仓库仅提供安装包、使用说明和问题反馈。

## 两种使用方式

| 功能 | 基础版 | 高级版 |
|---|---|---|
| 录音、选区覆盖、语音指令修改、AI 整理 | ✓ | ✓ |
| 编辑、撤销、复制、填入 Agent／二次下发 | ✓ | ✓ |
| 退出后保留当前正文、在线升级 | ✓ | ✓ |
| 草稿簿、投递历史 | — | ✓ |
| 约定词、纠正学习、代码口述 | — | ✓ |
| 选区翻译 | — | ✓ |

统一安装包默认是基础版，连接自己的模型 Key 即可使用。语音和文字处理的费用由相应服务商决定。**高级版在“设置 → 版本”输入兑换码解锁**；设备名额及期限以所获授权为准（本批公开体验码限 1 台），联网续验后最多离线使用 30 天。兑换码不包含模型额度。免费内测通道需另行开通，受服务方政策影响，可能暂停或不可用。

## 安装与开始

- **Windows 10／11，64 位**：完整解压便携 ZIP，打开其中的 `TalkMan.exe`。请保留相邻的 `_internal` 文件夹，不能只复制 EXE。此测试版尚未提供 Windows 商业代码签名。
- **Apple 芯片 Mac，macOS 14 及以上**：打开 DMG，把 TalkMan 拖到“应用程序”，再从那里运行。当前为未公证测试版，首次可能被系统拦截；确认来源后，在“系统设置 → 隐私与安全性”查看“仍要打开”。不需要关闭系统安全保护。
- 进入设置连接语音识别模型、文字改写模型。按“录音和覆盖”说话；选中一段后可直接覆盖或下达修改指令。选择目标后，“填入 Agent”只放进输入框，再次点击“下发”才发送。
- 首次录音允许麦克风。Mac 填入其他应用需辅助功能授权，可在应用的“Mac 授权”页检查。不同目标应用的兼容性可能有差异。

Mac 的红色关闭按钮只关闭窗口并保存正文，应用继续在后台运行。点击 Dock 图标或菜单栏“打开 Talk Man”可恢复；“退出 Talk Man”（⌘Q）才结束应用。设置会以独立窗口打开，切换项目时保持窗口大小。

## 在线升级

从 0.1.33 起，打开 **设置 → 版本 → 检查更新**，下载完成后点击 **保存并重启升级**。支持 Windows 完整便携目录和 Apple 芯片 Mac，草稿、模型 Key 和授权保留。启动时默认检查新版，只提醒，不自动安装；可在版本页关闭。

0.1.32 及更早版本需先从上方下载安装最新版。若 GitHub 暂时无法访问，检查或下载会提示重试，当前程序仍可继续使用。Mac 请先把应用放入“应用程序”；当前仍为未公证测试版，系统可能要求重新确认权限。

## 使用边界

允许个人及企业内部正常办公，也允许把写出的文字用于商业工作。未经书面授权，禁止转售、改名销售软件，或把软件集成到对外收费的产品／服务中。第三方组件按各自许可使用。

[软件使用协议](TalkMan-软件使用协议.md) · [隐私说明](TalkMan-隐私说明.md) · [第三方组件](TalkMan-第三方组件说明.md) · [版本记录](TalkMan-版本记录.md)

授权方：杭州行至云起科技有限公司。项目负责人：吴冬悦。

问题可在本仓库 Issues 提交，请附系统版本、Talk Man 版本和操作步骤。不要公开 API Key、兑换码或私密正文；换绑请联系发码维护者。
