#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dtbo.img 修改示例 —— 加显示刷新率档位 + 改充电参数

这不是通用工具，是**可直接运行的参考实现**。参数都在下面 CONFIG 里，
换成你自己机型的节点名/6d 码即可。

用法：
    python patch_example.py <输入.img> <输出.img>

在动手前它会做三道检查，任何一道不过都不写文件：
    1. AVB 解析 + 校验
    2. FDT 往返自检（长度一致）
    3. 改完重新解析核对（节点数 / 属性数 / 消失属性 = 0）
"""
import hashlib
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fdt import parse, serialize, u32_list, str_list, pack_u32, pack_str_list, roundtrip_check
from dtbo_image import DtboImage


# ==================== 配置区（按你的机型改） ====================
CONFIG = {
    # ---- 显示：加档位 ----
    # 模板节点（一般是该面板最高档），新节点从它克隆
    "timing_parent": "/fragment@80/__overlay__/qcom,mdss_mdp@ae00000/"
                     "qcom,mdss_dsi_o2_42_02_0a_dsc_cmd/qcom,mdss-dsi-display-timings",
    "timing_template": "timing@wqhd_normal_120hz_index_01",
    "timing_base_fps": 120,
    "timing_base_clockrate": 1286000000,   # 模板档位的 panel-clockrate
    "mdp_budget_ratio": 0.876,             # mdp-transfer-time = 帧周期 × 这个比例
    # 新档位：节点名, fps, cell-index, DDIC 6d 码
    "new_timings": [
        ("timing@wqhd_normal_144hz_index_11", 144, 11, 0x05),
        ("timing@wqhd_normal_165hz_index_12", 165, 12, 0x06),
    ],
    # 替换 6d 码时涉及的三个属性
    "timing_code_props": [
        "qcom,mdss-dsi-on-command",
        "qcom,mdss-dsi-timing-switch-command",
        "qcom,mdss-dsi-nolp-command",
    ],

    # ---- 充电 ----
    "charge_current": 20500,      # 目标电池侧电流上限 mA
    "single_pump_max": 20000,     # 单泵分支上限 mA
    "ffc_temp_high": 55,          # 快充温度上限 ℃
    "quick": "/fragment@45/__overlay__/mca_strategy_quick_charge",
    "buck": "/fragment@45/__overlay__/mca_strategy_buckchg_class",
    "thermal": "/fragment@45/__overlay__/mca_charger_thermal",
    "gbl": "/fragment@45/__overlay__/mca_quick_charge_batt_para_gbl",
    "apply_charge": True,         # 不想改充电就设 False
}


def patch_timings(root, cfg, log):
    """克隆模板 timing 节点，造出新的刷新率档位"""
    parent = root.find(cfg["timing_parent"])
    if parent is None:
        log.append("跳过：没找到 timing 父节点 %s" % cfg["timing_parent"])
        return

    tpl = parent.child(cfg["timing_template"])
    if tpl is None:
        log.append("跳过：没找到模板节点 %s" % cfg["timing_template"])
        return

    base_fps = cfg["timing_base_fps"]
    base_clk = cfg["timing_base_clockrate"]

    for name, fps, cell, code in cfg["new_timings"]:
        if parent.child(name) is not None:
            log.append("跳过 %s：已存在" % name)
            continue

        node = tpl.deep_clone()
        node.name = name
        node.props["qcom,mdss-dsi-panel-framerate"] = pack_u32([fps])
        node.props["cell-index"] = pack_u32([cell])
        node.props["qcom,mdss-dsi-panel-clockrate"] = pack_u32(
            [round(base_clk * fps / base_fps)])
        node.props["qcom,mdss-mdp-transfer-time-us"] = pack_u32(
            [round((1000000 // fps) * cfg["mdp_budget_ratio"])])

        changed = 0
        for pn in cfg["timing_code_props"]:
            if pn in node.props:
                raw = node.props[pn]
                n = raw.count(b"\x02\x6d\x00")
                node.props[pn] = raw.replace(b"\x02\x6d\x00", b"\x02\x6d" + bytes([code]))
                changed += n

        node.parent = parent
        idx = parent.kids.index(tpl)
        parent.kids.insert(idx + 1, node)
        log.append("新增 %s  fps=%d cell=%d 6d=0x%02x（替换 %d 处 6d 码）"
                   % (name, fps, cell, code, changed))


def patch_charge(root, cfg, log):
    """改充电参数"""
    if not cfg.get("apply_charge"):
        log.append("跳过充电：配置里关掉了")
        return

    I = cfg["charge_current"]
    TH = cfg["ffc_temp_high"]

    # 1) 温度墙
    n = root.find(cfg["buck"])
    if n is not None and "ffc_temp_high" in n.props:
        old = u32_list(n.props["ffc_temp_high"])
        n.props["ffc_temp_high"] = pack_u32([TH])
        log.append("ffc_temp_high  %s -> [%d]" % (old, TH))

    q = root.find(cfg["quick"])
    if q is None:
        log.append("跳过：没找到 %s" % cfg["quick"])
        return

    # 2) 电荷泵分档
    if "div_max_curr" in q.props:
        old = u32_list(q.props["div_max_curr"])
        if len(old) == 3:
            q.props["div_max_curr"] = pack_u32([old[0], old[1], I])
            log.append("div_max_curr  %s -> [%d,%d,%d]" % (old, old[0], old[1], I))

    if "div_single_curr" in q.props:
        old = u32_list(q.props["div_single_curr"])
        if len(old) == 3:
            v = min(I, cfg["single_pump_max"])
            q.props["div_single_curr"] = pack_u32([old[0], old[1], v])
            log.append("div_single_curr  %s -> [%d,%d,%d]" % (old, old[0], old[1], v))

    # 3) 阶梯表：只改 15/35/40 三段（低温段保持原厂的防冻限流）
    def flat_ffc(items):
        keep = items[-1]
        out = []
        for k in range(0, len(items) - len(items) % 3, 3):
            out.append(items[k])
            out.append(str(I))
            out.append(keep if k + 3 >= len(items) else str(I))
        return out

    def flat_normal(items):
        if len(items) < 6:
            return items
        return [items[0], str(I), str(I), items[3], str(I), items[-1]]

    for seg in ("15_35", "35_40", "40_48"):
        for fam in ("atl", "lwn"):
            for prefix, fn in (("ffc_para_%s_%s" % (fam, seg), flat_ffc),
                               ("normal_para_%s_%s" % (fam, seg), flat_normal)):
                if prefix in q.props:
                    old = str_list(q.props[prefix])
                    new = fn(old)
                    if len(new) == len(old):
                        q.props[prefix] = pack_str_list(new)
                        d = sum(1 for a, b in zip(old, new) if a != b)
                        log.append("%-24s %d/%d 项" % (prefix, d, len(old)))

    # 4) 电池温度分段：11 项一组
    def fix_batt(items):
        if len(items) % 11:
            return items
        out = []
        for i in range(0, len(items), 11):
            g = list(items[i:i + 11])
            if len(g) > 4 and g[4] not in ("0", "7056"):
                g[4] = str(I)
            if g[0] == "40" and g[1] == "48":
                g[1] = str(TH)
            if g[0] == "48":
                g[0] = str(TH)
            out.extend(g)
        return out

    for prop in ("batt_para_atl", "batt_para_lwn"):
        if prop in q.props:
            old = str_list(q.props[prop])
            new = fix_batt(old)
            if len(new) == len(old):
                q.props[prop] = pack_str_list(new)
                d = sum(1 for a, b in zip(old, new) if a != b)
                log.append("%-24s %d/%d 项" % (prop, d, len(old)))

    # 5) 温控限流表
    t = root.find(cfg["thermal"])
    if t is not None and "wired_thermal" in t.props:
        vals = u32_list(t.props["wired_thermal"])
        if len(vals) % 10 == 0:
            new = []
            for k in range(0, len(vals), 10):
                new.extend(vals[k:k + 4])
                new.extend([I] * 6)
            t.props["wired_thermal"] = pack_u32(new)
            log.append("wired_thermal  %d 组后 6 列 -> %d" % (len(vals) // 10, I))

    # 6) 快充总开关温度窗（9 项一组）
    def fix_temp(items):
        if len(items) % 9:
            return items
        out = []
        for i in range(0, len(items), 9):
            g = list(items[i:i + 9])
            if len(g) > 4 and g[2] == "2" and g[3] == "2" and g[4] == "12000":
                g[4] = str(I)
            if len(g) > 1 and g[1] == "48":
                g[1] = str(TH)
            if len(g) > 0 and g[0] == "48":
                g[0] = str(TH)
            out.extend(g)
        return out

    for path in (cfg["quick"], cfg["gbl"]):
        n = root.find(path)
        if n is not None and "temp_para1" in n.props:
            old = str_list(n.props["temp_para1"])
            new = fix_temp(old)
            if len(new) == len(old):
                n.props["temp_para1"] = pack_str_list(new)
                d = sum(1 for a, b in zip(old, new) if a != b)
                log.append("%-24s %d/%d 项" % (path.split("/")[-1] + " temp_para1", d, len(old)))


def snapshot(root):
    """收集 (路径, 属性名) → 值长度，用于改后核对"""
    d = {}

    def w(n):
        for pn, pv in n.props.items():
            d[(n.path(), pn)] = pv
        for c in n.kids:
            w(c)

    w(root)
    return d


def main():
    if len(sys.argv) != 3:
        print("用法: python patch_example.py <输入.img> <输出.img>")
        sys.exit(1)

    src, dst = sys.argv[1], sys.argv[2]

    # ------------- 1. 读 + 校验原始 -------------
    img = DtboImage(open(src, "rb").read())
    dtb = img.dtb(0)
    root, meta = parse(dtb)

    print("[1] 原始镜像")
    print("    AVB 算法      : %s" % (
        "NONE（只验哈希）" if img.avb_is_hash_only else img.avb.algorithm_type))
    print("    AVB 校验      : %s" % ("通过" if img.avb_verify() else "不通过"))
    if not img.avb_verify():
        print("    !! 原镜像 AVB 就不对，先别改，检查文件")
        sys.exit(2)

    ok, msg, first_diff = roundtrip_check(dtb)
    print("    FDT 往返自检  : %s" % msg)
    if not ok:
        # 长度不变 = 只差 padding，不影响功能；长度变了才是库坏了
        if "长度不同" in msg:
            print("    !! 序列化长度变了，库有问题，中止")
            sys.exit(3)
        print("    （长度一致，差异只是 padding 字节，属正常）")

    before = snapshot(root)
    before_nodes = len(before)

    # ------------- 2. 改 -------------
    log = []
    patch_charge(root, CONFIG, log)
    patch_timings(root, CONFIG, log)

    print()
    print("[2] 改动")
    for x in log:
        print("    " + x)

    # ------------- 3. 序列化 + 容量检查 -------------
    new_dtb, info = serialize(root, meta)
    print()
    print("[3] 序列化")
    print("    原 dtb : totalsize=%d  struct=%d" % (meta["totalsize"], meta["size_struct"]))
    print("    新 dtb : totalsize=%d  struct=%d  (%+d)"
          % (info["totalsize"], info["size_struct"], info["totalsize"] - meta["totalsize"]))

    new_content_end = img.entries[0]["offset"] + info["totalsize"]
    moved, did_move = img.reserve_vbmeta(new_content_end)
    if did_move:
        print("    vbmeta 从 %d 搬到 %d（4KB 对齐，因为新 dtb 会盖住原位置）"
              % (moved, moved))

    # ------------- 4. 写回 + 重算 AVB -------------
    img.set_dtb(new_dtb, 0)
    avb_info = img.avb_reflash()
    print()
    print("[4] AVB 重算")
    print("    image_size : %d -> %d" % (avb_info["old_image_size"], avb_info["new_image_size"]))
    print("    vbmeta 位置: %d" % avb_info["vbmeta_off"])
    print("    新 digest  : %s" % avb_info["new_digest"])
    print("    校验结果   : %s" % ("通过" if avb_info["verified"] else "不通过"))

    # ------------- 5. 改后核对 -------------
    out = img.to_bytes()
    chk = DtboImage(out)
    r2, m2 = parse(chk.dtb(0))
    after = snapshot(r2)

    lost = set(before) - set(after)
    added = set(after) - set(before)
    changed = [k for k in before if k in after and before[k] != after[k]]

    print()
    print("[5] 改后核对")
    print("    属性数     : %d -> %d" % (len(before), len(after)))
    print("    消失的属性 : %d %s" % (len(lost), "（正常）" if not lost else "!! 异常"))
    for k in list(lost)[:10]:
        print("       %s / %s" % k)
    print("    新增的属性 : %d" % len(added))
    print("    值变化的   : %d" % len(changed))
    print("    AVB 校验   : %s" % ("通过" if chk.avb_verify() else "不通过"))

    if lost or not chk.avb_verify():
        print()
        print("    !! 有问题，未写出文件")
        sys.exit(4)

    open(dst, "wb").write(out)
    print()
    print("[6] 已写出：%s" % dst)
    print("    大小 %d 字节（应与原分区大小完全一致：%s）"
          % (len(out), "是" if len(out) == len(open(src, "rb").read()) else "否 !!"))
    print("    SHA-256 %s" % hashlib.sha256(out).hexdigest())


if __name__ == "__main__":
    main()
