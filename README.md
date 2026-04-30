# QR Air Gap

通过视觉编码在空气隔离（air-gapped）环境中传输文件。远程端将文件分块编码为 QR 码（或自定义灰度/彩色帧）并循环展示，本地端截取屏幕自动解码并重组还原。

支持单文件、目录递归、glob 模式匹配的批量传输，多文件自动切换，丢帧自动重传，大文件自动分片。

## 工作原理

```
远程服务器 (Linux, Python 3.7+)           本地机器 (macOS)
┌────────────────────────────┐          ┌────────────────────────────┐
│  sender.py                 │          │  receiver.py               │
│  文件→分块→QR/GrayN→轮播   │ 远程桌面  │  截屏→QR/GrayN解码→重组    │
│  Tkinter 窗口显示           │──视觉──→│  多线程: 抓取+解码分离      │
│                            │          │                            │
│  ◄── osascript 按键信号 ───│──键盘──│  自动切换 / 缺帧重传信号    │
└────────────────────────────┘          └────────────────────────────┘
```

### 传输流程

1. **发送端**读取文件 → 按 chunk_size 分块 → 每块编码为 QR 码或 GrayN 帧（含序号、CRC32 校验、文件名）→ Tkinter 窗口循环轮播。帧在后台多进程生成，不阻塞播放。
2. **发送端 V3 校准**：V3 协议在每个文件的第一帧发送校准帧（灰度级 × 颜色通道的矩阵），接收端自动检测校准帧、采样各颜色通道的灰度中心值后发送空格键给发送端跳过等待。若接收端未响应，发送端在 15 秒后自动开始。
3. **接收端**截取屏幕指定区域 → 多线程解码（先尝试 QR，失败则尝试 GrayN）→ CRC32 校验 → 按序号去重重组 → 写入文件
4. **多文件**：一个文件收齐后，接收端自动发 `n` 键切换发送端到下一个文件；最后一个文件完成后发送端显示 END 帧，接收端自动退出
5. **丢帧重传**：接收端检测到重复帧（说明 sender 已循环一轮）时，将缺失帧列表 range 编码后通过 osascript 按键发送给发送端，发送端进入重传模式只播放缺失帧
6. **大文件分片**：超过 `--split-size`（默认 1GB）的文件自动按段切分为 `.partNN`，接收端收齐所有分片后自动合并为完整文件并删除分片

## 环境要求

| | 远程端 (发送) | 本地端 (接收) |
|---|---|---|
| Python | 3.7+ | 3.9+ |
| 依赖 | Pillow ≥7.0, Tkinter (标准库) | pyzbar, opencv, mss, Pillow, Tkinter |
| 额外 (QR) | `qrcode_vendor/` (本地构建后传入) | macOS: `brew install zbar` |
| 额外 (V3) | numpy | — (已含 opencv/numpy) |
| 网络 | 无需联网 | 可联网 (装依赖用) |

## 快速开始

### 1. 本地：安装接收端依赖

```bash
# uv (推荐)
uv sync

# 或 pip
pip install pyzbar opencv-python-headless mss Pillow

# macOS 必需
brew install zbar
```

### 2. 部署到远程服务器

需要传入远程服务器**同一目录**的文件：

| 文件 | 说明 |
|------|------|
| `sender.py` | 发送端主程序 |
| `protocol.py` | 共用协议（v1/v2 编解码、base45、CRC32） |
| `visual_transport.py` | V3 协议（GrayN 帧编解码），`--protocol 3` 时需要 |
| `qrcode_vendor/` | qrcode 库及其依赖（`--protocol 1/2` 时需要） |

**方式 A：逐文件传输**

将上述 3 个文件/目录通过已有手段传入远程服务器。

**方式 B：打包为 base64 文本**

```bash
# 本地：打包
python3 encode_server_bundle.py
# 生成 server_bundle.b64

# 远程端：先传入 decode_server_bundle.py（小文件，可直接粘贴），然后传入 server_bundle.b64
python3 decode_server_bundle.py
# 解出 sender.py, protocol.py, visual_transport.py, qrcode_vendor/
```

远程端还需 Pillow（通常已有）：`pip install 'Pillow>=7.0'`

若使用 `--protocol 3`，远程端还需 numpy：`pip install numpy`

### 3. 远程端：启动发送

```bash
# 单文件
python3 sender.py -f <文件路径>

# 整个目录（递归）
python3 sender.py -d <目录路径>

# Glob 模式
python3 sender.py -g "**/*.py"
```

发送端参数：

| 参数 | 默认值 | 适用协议 | 说明 |
|------|--------|----------|------|
| `--protocol` | 1 | 全部 | 编码协议：`1`=JSON+base64, `2`=base45+alphanumeric, `3`=GrayN 视觉帧 |
| `--fps` | 3 | 全部 | 每秒切换帧数 |
| `--chunk-size` | 400 | V1/V2 | 每块字节数。V3 由 grid 尺寸自动决定 |
| `--box-size` | 10 | V1/V2 | QR 码每模块像素大小 |
| `--grid` | 160,96 | V3 | 数据网格尺寸 `W,H`（支持逗号、`x`、空格分隔） |
| `--module-size` | 4 | V3 | 每 module 渲染像素数。增大可提高远程桌面下的解码可靠性 |
| `--gray-levels` | 4 | V3 | 灰度级数：`4` 或 `8` |
| `--colors` | 1 | V3 | 颜色通道数：`1`=仅灰度, `2`=灰度+1色, `4`=灰度+RGB |
| `--color-channel` | R | V3 | `--colors 2` 时选用的颜色通道：`R`/`G`/`B` |
| `--split-size` | 1G | 全部 | 超过此大小的文件自动分片传输（支持 K/M/G 后缀） |
| `--session-id` | 随机 | 全部 | 固定 4 字符会话 ID（相同输入+相同参数=相同帧） |
| `--qr-workers` | 4 | 全部 | 并行帧生成进程数 |
| `--num-qr` | 1 | V1/V2 | \[实验] 同时显示的 QR 码数量。V3 不支持（自动忽略） |
| `--verbose` / `-v` | — | 全部 | 显示详细处理过程 |

窗口快捷键：

| 按键 | 功能 |
|------|------|
| `n` / `→` | 下一个文件 |
| `p` / `←` | 上一个文件 |
| `空格` | 暂停 / 恢复（校准帧阶段按空格立即开始传输） |
| `+` / `-` | 加速 / 减速 |
| `r` | 重置：从第 0 帧重新开始（退出重传模式） |
| `g` | 跳转到指定帧 |
| `q` / `Esc` | 退出 |

### 4. 本地：启动接收

```bash
# GUI 选择截取区域
python receiver.py -o ./received/

# 直接指定截取坐标（跳过 GUI）
python receiver.py --region LEFT,TOP,WIDTH,HEIGHT -o ./received/
```

接收端参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--fps` | 5 | 截屏频率（建议 ≥ 发送端 fps 的 2 倍） |
| `--decode-workers` | 2 | 解码线程数（QR 和 V3 共用） |
| `--outdir` / `-o` | `received/` | 输出目录 |
| `--no-auto-next` | — | 禁用自动发送 `n` 键和缺帧重传 |
| `--timeout` | 15 | 多少秒无新帧后触发重传 |
| `--gray-levels` | 4 | V3 灰度级（4 或 8），从校准帧自动检测 |
| `--colors` | 1 | V3 颜色通道数（1/2/4），从校准帧自动检测 |
| `--grid` | 160,96 | V3 解码的数据网格尺寸（`W,H` / `WxH` / `W H`）。**须与发送端一致** |
| `--debug` | — | 保存前 20 帧到 `debug_frames/` |
| `--verbose` / `-v` | — | 显示详细解码信息（含 decode 失败原因） |
| `--num-qr` | 1 | \[实验] 每帧解码的 QR 码数量（仅 V1/V2） |

> 接收端**自动检测协议**：每帧先尝试 QR 解码（V1/V2），失败后尝试 GrayN 解码（V3）。无需手动指定协议版本。

接收端自动按原始文件名（含子目录结构）保存到 `--outdir`。大文件分片自动合并。

终端实时进度（含流水线 FPS 统计）：
```
[a3f1] [################--------------] 12/18 (67%) 1.2KB/s  capture=10.0  decode ok=4.8  testdata.txt
```

解码失败时自动输出诊断信息（无需 `--verbose`，首次连接时自动打印）：
```
[DECODE FAIL] v2: v2.1 crc mismatch: ... / v1: json parse: ...
  payload(640 chars): 2ABCD0000001200001500...
```

## 多文件工作流

```
发送端                              接收端
  │ 循环播放文件1 帧               │ 自动截屏解码
  │ ◄────────────────────────────── │ 收齐文件1 → 保存
  │                                 │ 自动发 'n' 键
  │ 收到 'n' → 切到文件2             │
  │ 循环播放文件2 帧               │ 检测新session → 开始接收
  │ ◄────────────────────────────── │ 收齐文件2 → 保存
  │                                 │ 自动发 'n' 键
  │ 最后一个文件收到 'n'             │
  │ 显示 END 帧 → 自动退出          │ 检测 END → 自动退出
```

## 大文件分片与合并

```
发送端                              接收端
  │ 3GB 文件 → 自动分为 3 段         │
  │   big.tgz.part1 (1GB)           │
  │   big.tgz.part2 (1GB)           │
  │   big.tgz.part3 (1GB)           │
  │                                 │
  │ 逐段发送                        │ 逐段保存
  │   播放 part1 帧                 │ 收齐 part1 → 保存
  │   自动切换 part2 ...            │ 收齐 part2 → 保存
  │   自动切换 part3 ...            │ 收齐 part3 → 保存
  │                                 │ 检测所有分片到齐 → 自动合并
  │                                 │ 生成 big.tgz，删除 .partNN 文件
```

- 默认分片阈值 1GB，可通过 `--split-size` 调整（支持 K/M/G/T 后缀）
- 分片对接收端透明：自动检测 `.partNN` 后缀，等所有连续分片到齐后合并
- 分片保留原始目录结构（`--dir` 模式下子目录中的大文件分片名也含路径前缀）

## 丢帧重传机制

```
发送端                              接收端
  │ 循环播放全部帧                   │ 逐帧解码，记录每个 chunk 的时间戳
  │                                 │ 收到重复 chunk，距上次 > 10s
  │                                 │ → 判断 sender 已循环一轮
  │                                 │ 计算缺失帧列表
  │                                 │ range编码: [1,2,3,5,6,8] → "1-3,5-6,8"
  │ ◄──osascript按键信号──────────── │ 发送 "m1-3,5-6,8,1-3,5-6,8."
  │ 解析range + 二次校验(数据发两遍) │
  │ 进入重传模式：只循环缺失帧       │ 继续解码，收齐后自动切换下一个文件
```

**触发逻辑（双层）：**

1. **时间戳 cycle detection（主触发）**：每个 chunk 被解码时记录时间戳。当同一 chunk 再次出现且距上次 > 10 秒，说明 sender 已循环一圈，立即触发重传。与 fps/num-qr/decode-workers 参数无关，不受高并发影响。

2. **Timeout 兜底（次触发）**：若超过 `--timeout`（默认 15 秒）未收到任何新 chunk，无论 cycle detection 是否触发，均强制发起重传。适用于小文件（循环周期 < 10 秒）等 cycle detection 不会生效的场景。

- 缺帧列表超长时自动拆分为多批次发送（首批 `m` 前缀替换，后续 `a` 前缀追加）
- 发送端 15 秒输入超时保护，防止信号不完整导致卡死

## 自动降级机制

当某些帧因远程桌面压缩等原因持续 CRC 校验失败时，发送端会自动降级协议参数以提高解码成功率：

```
发送端                              接收端
  │ 缺失帧列表连续 3 次完全一致       │
  │ → 触发降级                       │
  │ 降级链: 4c/G8 → 1c/G8 → 1c/G4   │
  │                                  │
  │ 清除当前文件缓存                  │
  │ 用降级参数重新编码帧              │
  │ 发送新的校准帧                    │ 自动检测新的校准帧参数
  │ ◄── 空格键 ──────────────────── │ (灰度级和颜色数自动适配)
  │ 开始发送降级后的数据帧            │ 以新参数解码
  │                                  │
  │ 切换到下一文件/分片时             │
  │ → 自动恢复原始参数               │
```

**降级链**（按严重程度逐级降低）：

| 原始参数 | 降级步骤 |
|---------|---------|
| 4c × Gray8 | → 1c × Gray8 → 1c × Gray4 |
| 4c × Gray4 | → 1c × Gray4 |
| 2c × Gray8 | → 1c × Gray8 → 1c × Gray4 |
| 2c × Gray4 | → 1c × Gray4 |
| 1c × Gray8 | → 1c × Gray4 |
| 1c × Gray4 | （已是最低，无法降级） |

- 降级**仅影响当前文件/分片**，切换到下一文件后自动恢复原始参数
- 接收端自动适配：校准帧检测同时尝试多种灰度级（4/8）和颜色模式的组合
- 发送端状态栏显示当前参数和降级等级（如 `[1c×gray4] [degraded L2]`）

## v2 协议（base45 + QR alphanumeric）

使用 `--protocol 2` 启用高效编码。接收端自动识别，无需配置。

```bash
# v2 协议 — 同样的 QR 复杂度下传输更多数据
python3 sender.py -f <文件路径> --protocol 2

# 提高 chunk-size 充分利用 v2 优势
python3 sender.py -f <文件路径> --protocol 2 --chunk-size 600
```

| | v1 (默认) | v2 |
|---|---|---|
| 编码方式 | JSON + base64 | 固定头 + base45 |
| QR 数据模式 | byte (8 bits/char) | alphanumeric (5.5 bits/char) |
| 400B chunk → QR 版本 | v17 (85×85) | v14 (73×73) |
| 同版本 QR 可装载 | 400 bytes | **600 bytes (+50%)** |
| idx/total 最大值 | 无限制 (JSON) | 99,999,999 (~40GB @ 400B/chunk) |
| 接收端改动 | — | 无需改动（自动识别） |

> v2 利用 base45 编码（RFC 9285）将二进制数据映射到 QR alphanumeric 字符集。虽然 base45 字符数比 base64 多（50% vs 33% 膨胀），但 QR alphanumeric mode 每字符仅需 5.5 bits（vs byte mode 的 8 bits），总比特数反而减少 ~27%。
>
> 当文件的 chunk 数超过 v2 上限时，sender 会自动降级到 v1 并打印警告。

## v3 协议（灰度 / 彩色视觉帧）

使用 `--protocol 3` 启用自定义视觉帧编码。支持灰度级（4 或 8）和多颜色通道（1/2/4），通过组合获得不同的数据密度：

| 颜色数 | 灰度级 | 每 module 比特数 | 160×96 每帧容量 |
|--------|--------|-----------------|----------------|
| 1 (灰度) | 4 | 2 | ~3817 B |
| 1 (灰度) | 8 | 3 | ~5737 B |
| 2 (灰+1色) | 4 | 3 | ~5737 B |
| 2 (灰+1色) | 8 | 4 | ~7657 B |
| 4 (灰+RGB) | 4 | 4 | ~7657 B |
| 4 (灰+RGB) | 8 | **5** | **~9577 B** |

```bash
# 发送端 — Gray4（默认），每帧约 3.8 KB
python3 sender.py -f <文件路径> --protocol 3

# 发送端 — Gray8，每帧约 5.7 KB
python3 sender.py -f <文件路径> --protocol 3 --gray-levels 8

# 发送端 — 4色 × Gray4，每帧约 7.7 KB
python3 sender.py -f <文件路径> --protocol 3 --colors 4

# 发送端 — 4色 × Gray8，每帧约 9.6 KB（最高密度）
python3 sender.py -f <文件路径> --protocol 3 --colors 4 --gray-levels 8

# 发送端 — 2色（灰+绿）× Gray4
python3 sender.py -f <文件路径> --protocol 3 --colors 2 --color-channel G

# 自定义 grid 尺寸（更大 grid = 更大容量）
python3 sender.py -f <文件路径> --protocol 3 --grid 200,120

# 接收端 — 自动检测灰度级、颜色数和协议
python receiver.py -o ./received/

# 如果发送端使用了非默认 grid，接收端必须指定相同值
python receiver.py -o ./received/ --grid 200,120
```

> **注意**：`--grid` 必须两端一致（支持逗号、`x`、空格分隔）。V3 不需要 qrcode 库，只需 numpy + Pillow。V3 不支持 `--num-qr` 多帧并行（自动忽略）。

### 颜色通道

V3 支持三种颜色模式，通过 `--colors` 参数选择：

| 模式 | 参数 | 符号空间 | 说明 |
|------|------|---------|------|
| 仅灰度 | `--colors 1` | N 级灰度 | 默认。R=G=B，每 module 编码 log₂(N) 位 |
| 灰+1色 | `--colors 2` | 2N 级 | 灰度 + 1 个 RGB 通道（`--color-channel R/G/B`） |
| 灰+RGB | `--colors 4` | 4N 级 | 灰度 + 红 + 绿 + 蓝，独立编码 |

**符号编码**：`sym = palette_idx × n_levels + level`
- palette 0 = 灰度：R=G=B=gray_lut[level]（0..255 等分）
- palette 1/2/3 = 颜色通道：仅对应的 R/G/B 通道有值（48..255 等分），其余通道为 0

**XOR mask** `sym ^= (n_levels - 1)` 只翻转 level 位，保留颜色位——天然兼容所有颜色模式。

### 校准帧

V3 协议在每个文件传输前自动发送一帧**校准帧**。校准帧的数据区域被划分为 n_levels 行 × n_colors 列的矩阵：

```
单色模式 (--colors 1)：      多色模式 (--colors 4)：
┌───────────────┐           ┌────┬────┬────┬────┐
│   Gray Lv0    │           │ K0 │ R0 │ G0 │ B0 │
├───────────────┤           ├────┼────┼────┼────┤
│   Gray Lv1    │           │ K1 │ R1 │ G1 │ B1 │
├───────────────┤           ├────┼────┼────┼────┤
│   Gray Lv2    │           │ K2 │ R2 │ G2 │ B2 │
├───────────────┤           ├────┼────┼────┼────┤
│   Gray Lv3    │           │ K3 │ R3 │ G3 │ B3 │
└───────────────┘           └────┴────┴────┴────┘
```

- 接收端采样每个 (颜色, 灰度级) 单元，获取各通道实际中心值和颜色饱和度阈值
- 检测到校准帧后，自动发送空格键给发送端（延迟 1 秒确保所有解码线程校准完成）
- 若接收端未响应，发送端 15 秒后自动开始
- 每个文件/分片的校准独立进行
- 2 色模式下接收端自动从校准帧检测使用的颜色通道

### 与 QR 对比

| | v1 (QR) | v2 (QR) | v3 Gray4 | v3 4c×Gray8 |
|---|---|---|---|---|
| 编码方式 | JSON + base64 | base45 + alphanumeric | 4 级灰度 | 4色 × 8级灰度 |
| 每帧有效载荷 | ~400 B | ~600 B | **~3800 B** | **~9600 B** |
| 数据密度 | 1 bit/module | ~1.45 bits/module | **2 bits/module** | **5 bits/module** |
| 帧内纠错 | Reed-Solomon | Reed-Solomon | 无（CRC32 + 重传） | 无（CRC32 + 重传） |
| 容量上限 | QR V40 ~2953 B | QR V40 ~4296 B | **无限制** | **无限制** |
| 远程端依赖 | qrcode + Pillow | qrcode + Pillow | numpy + Pillow | numpy + Pillow |

### 帧结构

```
+--------- quiet zone (4 modules) ---------+
|  [finder 7x7]  guard(1)  DATA  guard(1)  [finder 7x7]  |
|  guard(1)                                  guard(1)      |
|  DATA                                      DATA          |
|  guard(1)                                  guard(1)      |
|  [finder 7x7]  guard(1)  DATA  guard(1)  [finder 7x7]  |
+--------- quiet zone (4 modules) ---------+
```

- 四角定位图案（7×7 嵌套方块：黑-白-黑，与 QR finder 相同比率 1:1:3:1:1）
- 灰度通道映射：linspace(0, 255, n_levels)，颜色通道映射：linspace(48, 255, n_levels)
- 固定 XOR mask `(x+y)%3==0` 防止大面积相同灰度
- V3 二进制包头 23 字节：version, SID, idx, total, CRC32, filename, datalen
- 版本字节编码 (n_colors, n_levels)：`0x03`=1c/G4, `0x08`=1c/G8, `0x24`=2c/G4, `0x28`=2c/G8, `0x44`=4c/G4, `0x48`=4c/G8

### 容量参考

默认 Grid 160×96（15360 modules），不同模式下的每帧有效载荷：

| 模式 | bpm | 帧容量 | 有效载荷 | @5fps 吞吐 |
|------|-----|--------|---------|-----------|
| 1c × Gray4 | 2 | 3840 B | ~3817 B | ~19 KB/s |
| 1c × Gray8 / 2c × Gray4 | 3 | 5760 B | ~5737 B | ~28 KB/s |
| 2c × Gray8 / 4c × Gray4 | 4 | 7680 B | ~7657 B | ~38 KB/s |
| 4c × Gray8 | 5 | 9600 B | ~9577 B | **~48 KB/s** |

> 最高配置 (4c×Gray8, 160×96, 5fps) 吞吐量约 **48 KB/s**，是 QR v1 (2 KB/s) 的 **24 倍**

## 多QR码并行传输（实验性）

通过同时显示/捕获多个 QR 码来成倍提升传输速率。

```bash
# 发送端：同时显示 4 个 QR 码 (2x2 网格)
python3 sender.py -f <文件路径> --num-qr 4

# 接收端：同时解码多个 QR 码
python receiver.py -o ./received/ --num-qr 4
```

| 参数 | 发送端效果 | 接收端效果 |
|------|-----------|-----------|
| `--num-qr 1` (默认) | 单 QR 码显示 | 单 QR 码解码（性能最优） |
| `--num-qr 2` | 2 个 QR 码横排 | 每帧解码所有 QR 码 |
| `--num-qr 4` | 2×2 网格 | 每帧解码所有 QR 码 |

> **注意**：多 QR 模式为实验性功能。QR 码过多/过密可能降低识别率，建议配合降低 `--chunk-size`（如 200）或增大 `--box-size` 使用。

## 项目结构

```
QRAirGap/
├── sender.py                # [远程端] 发送器（QR/GrayN 帧生成 + Tkinter播放 + 灰度校准）
├── protocol.py              # [两端共用] V1/V2 协议（分块、编解码、base45、CRC32）
├── visual_transport.py      # [两端共用] V3 协议（GrayN 帧编解码、finder 检测、校准帧）
├── receiver.py              # [本地端] 接收器（QR + GrayN 自动解码 + 重传 + 分片合并）
├── encode_server_bundle.py  # [本地端] 打包 sender+protocol+visual_transport 为 base64
├── decode_server_bundle.py  # [远程端] 解包 server_bundle.b64
├── qrcode_vendor/           # qrcode 库及依赖（pypng, typing_extensions）
├── pyproject.toml           # 项目元数据 + 本地端依赖
└── README.md
```

## 调优建议

- **识别不了 QR 码**：降低 `--chunk-size`（如 200），或增大 `--box-size`（如 15）
- **传输太慢**：提高 `--fps`，同时提高接收端 `--fps` 和 `--decode-workers`；使用 `--protocol 3` + `--colors 4 --gray-levels 8` 可达 **~48 KB/s**（QR v1 的 24 倍）
- **大文件**：v1 ≈ 1.2KB/s；v2 ≈ 1.8KB/s；**v3 1c/G4 ≈ 19KB/s**；**v3 4c/G8 ≈ 48KB/s**（@5fps）
- **远程桌面模糊**：QR 降低 `--chunk-size`；V3 降低 `--grid`（如 80x48）或增大 `--module-size`（如 6）；优先使用 Gray4 单色（灰度级差大，抗压缩强）
- **V3 解码失败率高**：增大 `--module-size` 提高像素冗余；单色 Gray4 间距 85，JPEG Q50+ 即可；多色模式需更高质量连接（颜色通道受色度子采样影响）
- **多色模式 CRC 失败多**：降级为 `--colors 1`（纯灰度），或增大 `--module-size`；JPEG 4:2:0 色度子采样会降低颜色分辨率
- **QR 生成时 UI 卡顿**：属正常现象，后台多进程生成完毕后 UI 帧率恢复正常（V3 帧生成远快于 QR）
- **接收端调试**：使用 `--debug` 保存截屏帧到 `debug_frames/`，检查捕获区域是否正确；使用 `-v` 查看每次解码失败的具体原因
- **sender 窗口透明**：已强制设置 `-alpha 1.0`；若仍有问题，检查远程桌面的合成器设置（如关闭 compiz 透明效果）
