# QR Air Gap

通过 QR 码在空气隔离（air-gapped）环境中传输文件。远程端将文件分块编码为 QR 码并循环展示，本地端截取屏幕自动解码并重组还原。

支持单文件、目录递归、glob 模式匹配的批量传输，多文件自动切换。

## 工作原理

```
远程服务器 (Linux, Python 3.7+)         本地机器 (macOS)
┌──────────────────────────┐          ┌──────────────────────────┐
│  sender.py               │          │  receiver.py             │
│  文件→分块→QR码→循环轮播  │ 远程桌面  │  截屏→解码→校验→重组→保存 │
│  Tkinter 窗口显示         │──视觉──→│  多线程: 抓取+解码分离    │
└──────────────────────────┘          └──────────────────────────┘
```

### 传输流程

1. **发送端**读取文件 → 按 400 字节分块 → 每块编码为 QR 码（含序号、CRC32 校验、文件名）→ 统一 QR 版本 → Tkinter 窗口循环轮播
2. **接收端**截取屏幕指定区域 → 多策略解码 QR 码（pyzbar + OpenCV）→ CRC32 校验 → 按序号去重重组 → 写入文件
3. **多文件**：一个文件收齐后，接收端自动发 `n` 键切换发送端到下一个文件；最后一个文件完成后发送端显示 END 帧，接收端自动退出

## 环境要求

| | 远程端 (发送) | 本地端 (接收) |
|---|---|---|
| Python | 3.7+ | 3.9+ |
| 依赖 | Pillow (已有), Tkinter (标准库) | pyzbar, opencv, mss, Pillow, Tkinter |
| 额外 | `qrcode_vendor.zip` (本地构建后传入) | macOS: `brew install zbar` |
| 网络 | 无需联网 | 可联网 (装依赖用) |

## 快速开始

### 1. 本地：安装接收端依赖

```bash
pip install -r requirements.txt
brew install zbar  # macOS 必需
```

### 2. 本地：构建 qrcode 依赖包

```bash
python build_qrcode_zip.py
# 生成 qrcode_vendor.zip (~90KB)
```

### 3. 传输到远程服务器

需要传入远程服务器**同一目录**的文件（共 3 个）：

| 文件 | 大小 | 说明 |
|------|------|------|
| `sender.py` | ~12KB | 发送端主程序 |
| `protocol.py` | ~3KB | 共用协议（编解码、CRC32） |
| `qrcode_vendor.zip` | ~90KB | qrcode 库及其依赖（pypng, typing_extensions） |

这 3 个文件通过你已有的字符发送项目传入远程服务器即可。无需在远程端安装任何额外依赖。

### 4. 远程端：启动发送

```bash
# 单文件
python3 sender.py -f <文件路径>

# 整个目录（递归）
python3 sender.py -d <目录路径>

# Glob 模式
python3 sender.py -g "**/*.py"
```

常用参数：
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--fps` | 3 | 每秒切换帧数 |
| `--chunk-size` | 400 | 每块字节数（越小 QR 越简单越容易识别） |
| `--box-size` | 10 | QR 码像素大小 |

窗口快捷键：
| 按键 | 功能 |
|------|------|
| `n` / `→` | 下一个文件 |
| `p` / `←` | 上一个文件 |
| `空格` | 暂停 / 恢复 |
| `+` / `-` | 加速 / 减速 |
| `q` / `Esc` | 退出 |

### 5. 本地：启动接收

```bash
# GUI 选择截取区域
python receiver.py -o ./received/

# 直接指定截取坐标（跳过 GUI）
python receiver.py --region LEFT,TOP,WIDTH,HEIGHT -o ./received/
```

常用参数：
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--fps` | 5 | 截屏频率（建议 ≥ 发送端 fps 的 2 倍） |
| `--decode-workers` | 2 | 解码线程数 |
| `--outdir` / `-o` | `received/` | 输出目录 |
| `--no-auto-next` | — | 禁用自动发送 `n` 键 |
| `--debug` | — | 保存前 20 帧到 `debug_frames/` |

接收端自动按原始文件名（含子目录结构）保存到 `--outdir`。

终端实时进度：
```
[a3f1] [################--------------] 12/18 (67%) @5fps  testdata.txt
```

## 多文件工作流

```
发送端                              接收端
  │ 循环播放文件1 QR码               │ 自动截屏解码
  │ ◄────────────────────────────── │ 收齐文件1 → 保存
  │                                 │ 自动发 'n' 键
  │ 收到 'n' → 切到文件2             │
  │ 循环播放文件2 QR码               │ 检测新session → 开始接收
  │ ◄────────────────────────────── │ 收齐文件2 → 保存
  │                                 │ 自动发 'n' 键
  │ 最后一个文件收到 'n'             │
  │ 显示 END QR码 → 自动退出         │ 检测 END → 自动退出
```

## 项目结构

```
QRAirGap/
├── sender.py              # [远程端] 发送器
├── protocol.py            # [远程端+本地] 共用协议
├── receiver.py            # [本地端] 接收器（多线程）
├── build_qrcode_zip.py    # [本地端] 构建 qrcode 依赖 zip
├── requirements.txt       # 本地端 Python 依赖
├── pyproject.toml         # 项目元数据
└── qrcode_vendor.zip      # (构建生成) 远程端 qrcode 依赖
```

## 调优建议

- **识别不了 QR 码**：降低 `--chunk-size`（如 200），或增大 `--box-size`（如 15）
- **传输太慢**：提高 `--fps`，同时提高接收端 `--fps` 和 `--decode-workers`
- **大文件**：400B/chunk × 3fps ≈ 1.2KB/s，100KB 文件约需 80 秒
- **远程桌面模糊**：降低 `--chunk-size` 让 QR 码更稀疏，更容易被识别
