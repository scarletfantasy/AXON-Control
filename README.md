# AXON Control

独立实现的 NUX AXON 3 Windows 控制软件。提供七段 EQ 编辑、预设管理、A/B 试听和电脑播放声音的实时频谱。当前源码版本为 **0.21**，采用 MIT 许可证。

Independent Windows USB-MIDI editor for NUX AXON 3, with EQ editing, preset management, A/B comparison, and a live playback spectrum.

## 功能

- 七段 EQ：HP、LF、LMF、MF、HMF、HF、LP；参数输入、曲线拖动、撤销/重做、频段复制与粘贴。
- 修改预览、参考曲线、A/B 对比；明确区分本地编辑、写入当前工作参数与保存到音箱。
- 读取与切换设备预设，导入/导出备份，本地预设库与会话恢复。
- 电脑播放声音的 WASAPI 回环分析：流光曲线、能量柱、峰值保持、左右声道电平、音源选择与暂停。
- 深色界面、抗锯齿绘制、系统托盘、可选开机启动和设备重连。
- 原生最大化/还原、按当前显示器避开任务栏、状态同步图标、缩放事件合并与完整画面替换。
- 写入前备份、槽位与参数一致性检查、读回验证及部分写入失败后的恢复处理。

## 环境与启动

目前支持 Windows，使用 Python 3.10 或更新版本，需要 Tkinter。推荐使用标准的 64 位 Python 安装包，启用 Tcl/Tk 支持。USB 通信用 Windows WinMM，不依赖原版 AXON STUDIO。

在仓库目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\pythonw.exe "AXON Control.pyw"
```

Pillow 用于抗锯齿，NumPy 和 PyAudioWPatch 用于实时频谱；缺少这些可选包时，基础 USB 编辑仍可启动，相关显示功能会受限。

音箱通过 USB 连接后，点击“连接”。初次连接读取设备；编辑后点击“应用”写入工作参数，需要持久保存时再使用保存操作。开机启动功能需要打包版中的 `AXONControl.exe`。

实时频谱分析选中的**电脑播放设备**，显示的是数字播放信号。它不会测量音箱在房间中的实际声学响应，也不代表音箱内部 EQ 后的信号。没有音箱也可使用频谱；播放音乐后选择对应的输出设备即可。分析进程不保存录音文件。

## 打包成独立程序

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build-standalone.ps1 -Python .\.venv\Scripts\python.exe
```

结果位于 `dist/AXONControl/`。运行其中的 `AXONControl.exe`，分发时保留整个文件夹。脚本使用仓库相对位置，不要求特定安装路径。仓库提供源代码与构建方法，构建产物默认不进入 Git。

## 数据与隐私

设置、会话、备份、草稿和运行日志默认保存在 `%LOCALAPPDATA%\AXONControl`，可用环境变量 `AXON_CONTROL_DATA_DIR` 指定独立目录。发布源码不包含个人路径、真实设备抓包、用户预设、会话文件或日志。测试预设和报文由 [synthetic_fixtures.py](synthetic_fixtures.py) 根据公开字段格式生成。

分享诊断信息前，请检查日志与 JSON 备份中的文件路径、音频设备名称和预设名称。`.gitignore` 会排除常见的个人数据与构建产物；`python tools/check_public_tree.py` 可以检查待提交文件。

## 开发与测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -p "tests_*.py"
.\.venv\Scripts\python.exe tools/check_public_tree.py
```

测试使用模拟 MIDI 端口与合成数据，不会打开真实音箱或录制系统声音。完整桌面测试需要可交互的 Windows 桌面；自动化环境可设置 `AXON_CONTROL_SKIP_NATIVE_UI=1` 跳过两项依赖 Explorer 托盘的检查。其他测试照常运行。

更多说明见 [开发文档](docs/development.md) 与 [USB-MIDI 协议记录](docs/protocol.md)。

## 协议范围与限制

这是独立的电脑端实现，与 NUX / Cherub Technology 无隶属关系。仓库没有原版软件的二进制、反编译代码、固件镜像或固件更新功能。兼容范围以 AXON 3 和已识别的 `NFM-3` 设备为限。

协议记录来自设备通信观察、原版程序行为分析与已有实机验证。整体 EQ、改名与重置的部分语义主要依据静态分析和模拟测试，不能视为已全面实机验证。未知字节仍原样保留；未实现作用不明的命令 `0x07`。曲线显示使用估计的滤波模型，不能据此确认固件来源、实际 DSP 采样率或所有隐藏接口。

## 许可证

[MIT](LICENSE)。第三方运行依赖遵循各自许可证。
