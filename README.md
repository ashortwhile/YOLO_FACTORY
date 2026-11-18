# YOLO 工坊

一个本地运行的桌面应用，把 **数据集管理 → 图像标注 → 模型训练 → 推理验证** 串成一条流水线。后端是 Python 标准库 HTTP 服务，前端是原生 HTML/CSS/JS，无 Flask/FastAPI/前端框架依赖。所有数据与模型都留在本机，不上传。

## 功能

- **数据集**：新建 / 挂载已有 YOLO 目录 / 扫描同级目录，自动划分 train-val，校验类别与标签
- **打标签**：拖拽画框、移动、缩放、撤销重做（Ctrl+Z / Ctrl+Shift+Z）、滚轮缩放画布、空格+拖拽平移、快捷键切类别与翻页，支持自动保存与 AI 预标注
- **训练**：实时日志与 mAP 曲线，可调 epochs/imgsz/batch/优化器/学习率等，支持断点续训与随时停止，任务在独立子进程中运行
- **推理验证**：单图检测并可视化、验证 mAP、导出 ONNX / OpenVINO / TorchScript / TFLite
- **硬件自适应**：自动识别 NVIDIA / Intel / AMD / Apple 显卡，按驱动匹配并安装对应后端（CUDA / XPU / ROCm / MPS / DirectML），失败自动回退 CPU
- **任务管理**：后台任务列表、状态跟踪、日志增量读取

## 安装与使用（普通用户）

1. 拿到 `YOLO工坊-安装版.exe`，双击运行安装向导，选择安装目录。
2. 安装完成后，双击桌面或开始菜单的「YOLO 工坊」启动，直接进入应用，无需等待。
3. 浏览与管理数据不需要额外环境。要训练 / 预标注 / 推理时，打开「环境与显卡」页，点「运行环境」卡片的**安装运行环境**：软件会按显卡自动匹配并安装 PyTorch / Ultralytics / 默认权重（后台下载，可边等边用其它页面）。
4. 环境就绪后相关功能解锁，按「概览 → 数据集 → 打标签 → 训练 → 推理验证」的顺序使用即可。

安装器与启动器是两个独立的 exe：安装器只负责安装/卸载，安装目录下的 `YOLO工坊.exe` 才是启动器。卸载入口在开始菜单，也可通过系统「添加或删除程序」。

## 开发运行

需要 Python 3.9–3.13（推荐 3.10/3.11）。

```bash
# 1. 配置环境（创建 .venv、装依赖、探测显卡），只做一次
python bootstrap.py --setup-only

# 2. 启动本地服务（默认 127.0.0.1:8765）
python bootstrap.py
# 只启动服务、不开应用窗口：python bootstrap.py --no-browser
# 强制重装依赖：python bootstrap.py --force
```

浏览器打开 `http://127.0.0.1:8765/`。也可直接 `python server.py` 启动服务（前提：环境已配好）。

服务使用进程级随机 token 做本地 API 鉴权，token 随页面注入；token 随进程变化，服务重启后需刷新页面。

### 常用启动参数

| 程序 | 参数 |
| --- | --- |
| `bootstrap.py` | `--force` 重装依赖 · `--no-browser` 不开窗 · `--browser` 用普通浏览器标签 · `--setup-only` 只配环境 · `--port N` · `--mirror tsinghua\|aliyun\|pypi` |
| `server.py` | `--host` · `--port` · `--quiet` |

## 打包

在装有 PyInstaller 的 Windows 机器上双击 `打包安装版.bat`（或直接 `python build_setup.py`）。它会按三步产出 `YOLO工坊-安装版.exe`：

1. `build_exe.py`：把 `launcher.py` 打成独立启动器 `YOLO工坊.exe`
2. `make_release.py`：生成绿色版 ZIP（代码 + web + 启动器），作为安装包的 payload
3. PyInstaller 打包 `installer.py` 生成安装器，产出 `YOLO工坊-安装版.exe`

首次打包会自动安装 PyInstaller（走清华镜像）并生成图标/启动图。构建产物在 `dist/`、`build/`、`payload.zip` 和上级目录，均已 git 忽略。

## 测试

零第三方依赖，使用标准库 `unittest`：

```bash
python -m unittest discover -s tests -v
```

覆盖文件名与路径校验、配置原子读写、版本一致性，以及 API 鉴权（token / Origin / Host 校验、恶意参数、路径穿越、任意下载域名拦截）等回归用例。

## 目录结构

```
server.py           本地 HTTP 服务与 API
bootstrap.py        首次启动的环境自举（venv / 依赖 / 模型 / 服务）
launcher.py         独立启动器（打包成 YOLO工坊.exe）
installer.py        安装器 / 卸载器（打包成安装版 exe）
worker_task.py      训练/标注/推理/下载等子任务
worker_backend.py   显卡后端切换子任务
hardware.py         硬件探测与后端方案
env_utils.py        子进程环境变量清理
version.py          应用版本单一来源
config.json         本机配置（不入库）
build_exe.py        构建独立启动器
build_setup.py      构建安装版（串联三步）
make_release.py     生成绿色版 ZIP / payload
打包安装版.bat       一键构建入口
web/                前端（index.html / app.js / style.css）
tests/              单元测试
workspace/          用户数据：数据集 / 模型 / 导出 / 训练结果（不入库）
logs/               任务日志与结果（不入库）
```

## 说明

- 版本统一管理在 `version.py` 的 `APP_VERSION`，发版时只改这一处。
- 启动速度：启动只探测硬件并直接进应用（约 2 秒），不下载任何东西。运行环境由用户在「环境与显卡」页主动安装；安装后走快路径，只有切换显卡后端或距上次配置超过 7 天（`auto_update`）才会检查依赖更新。命令行需要强制重装/更新时用 `python bootstrap.py --force`。
- `config.json` 中的 `version` 字段是**配置格式版本**，与应用版本无关。
- 模型权重是 `.pt`（pickle）文件，请只从可信来源导入；应用已恢复 TLS 校验并限制下载域名，但不能替代你的判断。
