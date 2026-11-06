# 图标与字体来源

图标：官方 Lucide 1.8.0，https://lucide.dev ，https://github.com/lucide-icons/lucide 。使用官方已安装 JavaScript 包中的节点，渲染为透明 24×24 PNG；黑色、白色和录音状态红色各一组。来源许可证为 ISC，部分 Feather 衍生图标另适用 MIT；完整许可见同目录 `TalkMan-Lucide-LICENSE.txt`。

字体：Google Fonts 官方仓库的 Space Grotesk 可变字体和 Space Mono Regular，均为 SIL Open Font License 1.1。保留原始 TTF，不改字体内容；完整许可见 `fonts/TalkMan-SpaceGrotesk-OFL.txt` 与 `fonts/TalkMan-SpaceMono-OFL.txt`。

- Space Grotesk：https://github.com/google/fonts/tree/main/ofl/spacegrotesk
- Space Mono：https://github.com/google/fonts/tree/main/ofl/spacemono

字体加载：中文正文优先系统中文字体；Windows 支持只向当前进程注册随程序打包的 TTF，不修改系统字体安装。Mac 不注册字体，未安装 Space 字体时使用 PingFang SC、Helvetica Neue、Menlo 等系统字体。

「AI 整理一次」使用同库 Brain 大脑线条图标，24×24、2 像素圆头线条，与其他按钮一致。

程序运行和图标加载不需要网络。字体或图标缺失不阻止正文编辑。

0.1.26 新增「翻译全文」及「记住这个词」图标为本项目绘制的简洁线稿，沿用 24×24 网格和约 2 像素线宽；不增加字体或网络依赖。

0.1.27 新增删除选区（退格）与完成对勾图标，均为本项目自绘的 24×24 单色圆头线稿，约 1.75 像素线宽。

0.1.28 应用图标由本项目绘制：麦克风与文字光标组合，黑白轮廓；16–24 px 使用简化麦克风，Mac 菜单栏使用透明模板。素材位于 `app/`，可用 `TalkMan-生成应用图标.py` 离线重建，非外部品牌商标。
