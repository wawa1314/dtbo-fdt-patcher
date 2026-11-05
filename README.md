# dtbo-fdt-patcher

纯 Python 改 Android `dtbo.img` 的工具集 —— 解包、改设备树、重算 AVB、写回。

**不需要 dtc / avbtool / fastboot 之外的任何工具链。**

适用于 **AVB 只做哈希校验（`algorithm_type = NONE`）** 的高通机型。
典型的应用场景：给屏幕加刷新率档位、调整充电电流/温度阈值、改任何设备树属性。

---

## 这个工具能做什么

- 读出 `dtbo.img` 里的设备树，打印成可读的树/属性
- 按「节点路径 + 属性名」精确改值，也可以整节点克隆
- 自动处理 **dtb 变大会盖住 vbmeta** 的情况（把 vbmeta 往后搬，4KB 对齐）
- 重算 AVB digest 并写回
- 全程自检：AVB 校验、FDT 往返、改后核对（**消失的属性必须为 0**）

## 不能做什么

- 不能破签名（`algorithm_type != 0` 的镜像，改了会验签失败）
- 不保证 DDIC 认你编的寄存器码（比如给屏加 165Hz，面板固件不支持就是黑屏）
- 不改 `super` / `boot` / 任何非 dtbo 分区

---

## 快速开始

```bash
# 1. 先看原镜像长什么样
python scripts/inspect_dtbo.py dtbo.img

# 2. 看刷新档位 / 充电参数
python scripts/inspect_dtbo.py dtbo.img --timings
python scripts/inspect_dtbo.py dtbo.img --charge

# 3. 改（脚本里的 CONFIG 区改成你的参数）
python scripts/patch_example.py dtbo.img dtbo_mod.img

# 4. 刷
adb reboot bootloader
fastboot flash dtbo dtbo_mod.img
fastboot reboot
```

> ⚠️ `fastboot flash` 第一个参数是**分区名**（`dtbo`），不是文件名。
> 写成 `fastboot flash dtbo_mod` 会报 `Error flashing partition : 0000000E`。

---

## 文件说明

| 文件 | 作用 |
|---|---|
| `scripts/fdt.py` | FDT（设备树二进制）解析与序列化 |
| `scripts/dtbo_image.py` | DTBO 表 + AVB 结构处理 |
| `scripts/inspect_dtbo.py` | 分析工具（只读） |
| `scripts/patch_example.py` | 修改示例（可直接跑，改 CONFIG 即可） |

---

## 工作原理

### 1. DTBO 分区布局

```
[0 .. table_total)        DTBO 表头(32B) + 条目(32B × N)
                            条目：dtb_size(4) dtb_offset(4) id(4) rev(4) custom(16)
[.. vbmeta_offset)        各 dtb 内容
[vbmeta_offset .. +size)  AVB vbmeta
[.. footer_offset)        填充
[footer_offset .. +4096)  AVB footer  ← 位置在分区末尾 -64 字节
```

### 2. AVB 哈希

`algorithm_type = 0`（NONE）的镜像，`authentication_data_size = 0`，只有哈希没有签名：

```
digest = sha256(salt || image[0:image_size])
```

- `salt` 是 32 字节明文，存在 hash descriptor 里
- `image_size` 也存在描述符里
- **改完内容只要重算这一项写回，校验就能过**

这就是为什么这类镜像能自改 —— 没有签名拦着，只是完整性校验，而校验值是我们自己能算的。

### 3. 三个容易踩的坑

**坑 1：dtb 变大会盖住 vbmeta**

加两个 timing 节点，dtb 从 551KB 撑到 566KB。原 vbmeta 在 552,960 —— 被盖住了。

正确做法：**改内容之前先把 vbmeta 搬到 4KB 对齐的安全位置**。
先改再搬就晚了，旧 vbmeta 已经被覆盖，拷不出来了。
代码里是 `img.reserve_vbmeta(new_end)` 必须在 `img.set_dtb()` 之前调。

**坑 2：清空旧 vbmeta 区的时机**

`sha256` 覆盖 `[0, image_size)`，如果 image_size 扩大到了旧 vbmeta 的位置，
那块内容也会被算进哈希。所以要么让新内容完整覆盖它，要么先清零 —— 顺序错了哈希就对不上。

本库的处理：`avb_reflash()` 里先判断 `vbmeta_off >= content_end`，
满足才继续；然后清零 `[content_end, old_image_size)` 的残留 —— 这样哈希范围内不会混进脏数据。

**坑 3：FDT 字符串块的后缀寻址**

FDT 允许属性名的 `nameoff` 指向另一个字符串的**后半段**。例如属性 `gpio` 的偏移
可以指向 `qcom,pm8550-gpio` 里 `gpio` 的起始字节。

重建时如果不为所有后缀建映射，会有一批属性名找不到（实测 11 个）。

---

## 自检清单（这套流程的验收标准）

改完一个镜像，跑 `inspect_dtbo.py --all`，这些必须全过：

| 检查 | 期望 |
|---|---|
| DTBO 表 magic | `0xd7b7ab1e` |
| `table_total` | 等于 `dtb_offset + dtb_size` |
| AVB 校验 | `通过` |
| 三段布局 | 无重叠 |
| 往返测试 | 长度一致（padding 差异正常） |
| 消失的属性 | **0 个** ← 最关键 |
| 新增的属性 | 等于「新节点属性数 × 节点数」 |
| 输出大小 | 与原分区**完全一致** |

**「消失的属性」必须为 0** —— 这是最容易翻车的地方。
用 dict 存属性时，同名属性会被静默覆盖（比如 `__symbols__` 里的 698 个符号），
一旦有覆盖，跑起来就是设备树残缺、开不了机。

---

## 关于加刷新率档位

高通平台换刷新率，本质是改 DDIC 的 `6d` 寄存器：

```
15 00 00 40 00 00 02 6d XX
```

三个地方都要改（`XX` 换成目标码）：
- `qcom,mdss-dsi-on-command`（开机初始化）
- `qcom,mdss-dsi-timing-switch-command`（运行中切换）
- `qcom,mdss-dsi-nolp-command`（息屏唤醒后恢复）

**怎么知道该用哪个码？** 把现有档位的 `6d` 码列出来，找空号：

以小米 15 Pro（O2 屏）为例，已用：

| 码 | 档位 |
|---|---|
| `0x00` | 120Hz |
| `0x01` | 60Hz |
| `0x02` | 40Hz |
| `0x03` | 30Hz |
| `0x04` | 24Hz |
| `0x08` | 10Hz（idle） |
| `0x09` | 1Hz（idle） |
| `0x0a` | test |
| `0x0b` | 90Hz |
| `0x0c` | auto 120→30 |
| `0x0d` | auto 60→10 |

空号：`0x05`、`0x06`、`0x07`、`0x0e`、`0x0f`。

**⚠️ 空号不代表面板支持。** DDIC 固件里有没有对应的时序表，从 dtb 看不出来。
填了空号最坏情况是切过去黑屏 —— 这就是为什么加新档位要留好回滚手段。

### 除 6d 码外还要改的 4 个属性

新节点从现有档位**深拷贝**，只改这几处：

| 属性 | 怎么算 |
|---|---|
| `qcom,mdss-dsi-panel-framerate` | 目标帧率 |
| `cell-index` | 取没被占用的序号 |
| `qcom,mdss-dsi-panel-clockrate` | 按帧率等比放大：`基准 × 目标fps / 基准fps` |
| `qcom,mdss-mdp-transfer-time-us` | `(1e6 / fps) × 0.876`（0.876 是从原厂数据反推的经验值） |

其余属性（porch 时序、DSC 参数、所有命令表）**完全共用模板节点的** ——
这也说明改刷新率不需要动时序参数。

---

## 回滚

改之前先备份：

```bash
adb shell "dd if=/dev/block/by-name/dtbo_a of=/sdcard/dtbo_stock.img bs=4096"
adb pull /sdcard/dtbo_stock.img
```

（`dtbo_a` 按槽位替换，`getprop ro.boot.slot_suffix` 查）

回滚：

```bash
fastboot flash dtbo dtbo_stock.img
```

**黑屏了怎么办**：长按电源键 10 秒强制重启；还黑就进 fastboot 刷回备份。

---

## 已验证机型

| 机型 | 代号 | 结果 |
|---|---|---|
| 小米 15 Pro | haotian / O2 | 充电解限 + 144Hz/165Hz 两档，实机刷入开机正常 |

---

## 免责

刷机有风险，改设备树尤其。**先备份，先刷保守版本，确认能开机再加激进改动。**
本工具的作者不对任何变砖负责。

GPL-3.0

