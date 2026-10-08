# 开发说明

## 结构

| 文件 | 作用 |
| --- | --- |
| `axon_control.py`、`AXON Control.pyw` | Tk 界面与启动入口 |
| `axon_protocol.py`、`winmidi.py` | 独立 SysEx 编解码、事务校验与 Windows MIDI 传输 |
| `editor_state.py`、`editor_session.py` | 参数编辑、导入验证、撤销与会话恢复 |
| `audition_state.py`、`curve_reference.py` | A/B 试听与参考曲线 |
| `ui_widgets.py`、`smooth_render.py`、`curve_view.py` | 控件、抗锯齿与 EQ 曲线 |
| `audio_capture.py`、`audio_spectrum.py` | 独立 WASAPI 回环进程与 FFT 分析 |
| `audio_view.py`、`spectrum_style.py` | 频谱交互、渐变、能量柱与峰值保持 |
| `desktop_integration.py`、`app_lifecycle.py` | 托盘、启动选项与进程生命周期 |
| `user_data.py`、`preset_library.py`、`ui_runtime.py` | 数据存储、预设库与诊断 |
| `synthetic_fixtures.py`、`tests_*.py` | 合成预设、模拟设备与回归测试 |

## Windows 测试

安装 `requirements.txt` 后运行：

```powershell
python -m unittest discover -p "tests_*.py"
```

完整测试包含 Tk 控件布局、独立子进程退出、注册表隔离键以及原生托盘消息生命周期。开机启动检查只使用临时目录中的占位文件与随机测试注册表键，不操作实际的 Windows Run 项。

无人值守环境通常没有可用的 Explorer 托盘。设置 `AXON_CONTROL_SKIP_NATIVE_UI=1` 后仅跳过两项依赖可交互桌面的托盘检查。GitHub Actions 使用 Windows、Python 3.11，运行其余测试与发布内容检查。

调试新界面时，可设置 `AXON_CONTROL_DATA_DIR` 为临时目录，避免读取真实用户会话。不要将真实设备身份响应、音频设备名称、备份或诊断日志添加到测试夹具。新增测试使用合成数据，并保留独立的已知字节断言，避免仅验证编码器能够解码自己的输出。

## 构建

安装 `requirements-build.txt` 并执行 `build-standalone.ps1`。PyInstaller 产物位于忽略的 `build/`、`dist/`。`launcher.c` 是可选的源码启动器，只查询标准 Python 注册表安装项；独立打包不需要编译它。

源码提交不包含本机编译产物。需要发布二进制时，建议在干净的构建目录或 CI 中重新打包并检查资源、路径及第三方许可证；编译产物可能携带构建环境信息。

## 发布内容检查

```powershell
python tools/check_public_tree.py
git diff --cached --stat
git diff --cached
```

检查脚本扫描 Git 索引中的文件名与文本内容，阻止常见的日志、会话、抓包、编译产物、个人主目录路径、密钥文件和令牌格式。它不能发现所有敏感内容，提交前仍需查看实际差异。首次开源提交使用全新的 Git 历史以及 GitHub 隐私邮箱。
