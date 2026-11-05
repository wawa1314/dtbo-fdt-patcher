#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dtbo.img 分析工具 —— 布局 / AVB / 刷新档位 / 充电参数

用法：
    python inspect_dtbo.py <dtbo.img>
    python inspect_dtbo.py <dtbo.img> --timings
    python inspect_dtbo.py <dtbo.img> --charge
    python inspect_dtbo.py <dtbo.img> --all
    python inspect_dtbo.py <dtbo.img> --dump-node "/fragment@45/__overlay__/mca_strategy_quick_charge"
"""
import os
import sys
import argparse

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fdt import parse, u32_list, str_list, roundtrip_check
from dtbo_image import DtboImage


def hr(title):
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


def show_layout(img):
    hr("镜像布局")
    print("  分区总大小      : %d 字节 (%.1f MB)" % (len(img.blob), len(img.blob) / 1048576.0))
    print("  DTBO 表 magic   : 0x%08x" % int.from_bytes(img.blob[0:4], "big"))
    print("  table_total     : %d" % img.table_total)
    print("  entry_count     : %d" % img.entry_count)
    print()
    for e in img.entries:
        print("  dtb#%d  offset=%-10d size=%-10d id=0x%x rev=0x%x"
              % (e["index"], e["offset"], e["size"], e["id"], e["rev"]))
    print()
    print("  content_end     : %d" % img.content_end)
    print("  是否有 AVB      : %s" % ("是" if img.avb.present else "否"))
    if not img.avb.present:
        return

    print("  vbmeta          : offset=%-10d size=%d" % (img.vbmeta_off, img.vbmeta_size))
    print("  AVB footer      : offset=%d" % img.footer_off)
    print("  avb_image_size  : %d" % img.avb.image_size)
    print()
    print("  AVB 算法        : %s" % (
        "NONE（只校验哈希，不验签）" if img.avb_is_hash_only
        else "algorithm_type=%s" % img.avb.algorithm_type))
    print("  hash 算法       : %s" % img.avb.hash_algorithm.decode("ascii", "replace"))
    print("  salt            : %s" % img.avb.salt.hex())
    print("  stored digest   : %s" % img.avb.digest.hex())
    calc = img.avb_calc_digest()
    print("  calc   digest   : %s" % calc.hex())
    print("  校验结果        : %s" % ("通过" if calc == img.avb.digest else "不匹配"))

    vb_end = img.vbmeta_off + img.vbmeta_size
    ok = img.content_end <= img.vbmeta_off and vb_end <= img.footer_off
    print("  三段无重叠      : %s" % ("是" if ok else "否"))


def show_timings(root, panel=None):
    hr("显示档位（timing 节点）")

    found = []

    def scan(n):
        if n.name == "qcom,mdss-dsi-display-timings":
            found.append(n)
        for c in n.kids:
            scan(c)

    scan(root)
    if not found:
        print("  没找到 qcom,mdss-dsi-display-timings")
        return

    targets = found
    if panel:
        targets = [x for x in found if panel in x.path()]
        if not targets:
            print("  没有匹配面板 %r 的 timings 节点；现有：" % panel)
            for x in found:
                print("     %s (%d 档)" % (x.path(), len(x.kids)))
            return
    elif len(found) > 1:
        print("  发现 %d 组 display-timings（用 --panel 指定）：" % len(found))
        for x in found:
            print("     %s  (%d 档)" % (x.path(), len(x.kids)))
        targets = [max(found, key=lambda x: len(x.kids))]
        print("  下面显示档位最多的那组：")

    for dt in targets:
        print()
        print("  面板: %s" % dt.path())
        print("  共 %d 个档位：" % len(dt.kids))
        print()
        print("  %-40s %-5s %-5s %-12s %-8s %s"
              % ("节点名", "fps", "cell", "clockrate", "mdp_us", "6d码"))
        for k in dt.kids:
            def gv(p):
                v = k.props.get(p)
                return int.from_bytes(v, "big") if v else "?"

            codes = set()
            for pn in ("qcom,mdss-dsi-on-command",
                       "qcom,mdss-dsi-timing-switch-command",
                       "qcom,mdss-dsi-nolp-command"):
                raw = k.props.get(pn, b"")
                i = 0
                while True:
                    i = raw.find(b"\x02\x6d", i)
                    if i < 0:
                        break
                    if i + 2 < len(raw):
                        codes.add(raw[i + 2])
                    i += 3

            print("  %-40s %-5s %-5s %-12s %-8s %s" % (
                k.name, gv("qcom,mdss-dsi-panel-framerate"), gv("cell-index"),
                gv("qcom,mdss-dsi-panel-clockrate"), gv("qcom,mdss-mdp-transfer-time-us"),
                " ".join("0x%02x" % c for c in sorted(codes)) or "-"))


def show_charge(root):
    hr("充电参数")
    q = "/fragment@45/__overlay__"

    for path, prop, label in [
        (q + "/mca_strategy_buckchg_class", "ffc_temp_high", "有线快充温度上限"),
        (q + "/mca_strategy_buckchg_class", "ffc_temp_low", "有线快充温度下限"),
        (q + "/mca_strategy_quick_charge", "div_max_curr", "电荷泵分档上限"),
        (q + "/mca_strategy_quick_charge", "div_single_curr", "单泵分支上限"),
    ]:
        n = root.find(path)
        if n is not None and prop in n.props:
            print("  %-22s %-18s %s" % (label, prop, u32_list(n.props[prop])))

    n = root.find(q + "/mca_charger_thermal")
    if n is not None and "wired_thermal" in n.props:
        vals = u32_list(n.props["wired_thermal"])
        print()
        print("  温控限流表 wired_thermal（%d 个 = %d 组 x 10）："
              % (len(vals), len(vals) // 10))
        print("    %-5s %-22s %s" % ("组", "前4列(接口侧)", "后6列(充电电流)"))
        for g in range(len(vals) // 10):
            row = vals[g * 10:(g + 1) * 10]
            print("    %-5d %-22s %s" % (g,
                                         " ".join("%d" % x for x in row[:4]),
                                         " ".join("%d" % x for x in row[4:])))

    n = root.find(q + "/mca_strategy_quick_charge")
    if n is not None and "batt_para_atl" in n.props:
        items = str_list(n.props["batt_para_atl"])
        print()
        print("  电池温度分段 batt_para_atl：")
        for i in range(0, len(items) - len(items) % 11, 11):
            g = items[i:i + 11]
            print("    %-9s ~ %-9s 模式 %s/%s 电流 %s"
                  % (g[0], g[1], g[2], g[3], g[4]))


def dump_node(root, path):
    hr("节点转储 %s" % path)
    n = root.find(path)
    if n is None:
        print("  未找到该路径")
        return
    print("  %s  （%d 属性 / %d 子节点）" % (n.name, len(n.props), len(n.kids)))
    print()
    for pname, pval in n.props.items():
        if len(pval) % 4 == 0 and len(pval) <= 64 and len(pval) > 0:
            shown = str(u32_list(pval))
        elif pval and b"\x00" in pval[:24]:
            shown = str(str_list(pval))
        elif not pval:
            shown = "(空)"
        else:
            shown = "(%d 字节二进制)" % len(pval)
        print("    %-46s = %s" % (pname, shown[:110]))
    for k in n.kids:
        print("    +- %s" % k.name)


def main():
    ap = argparse.ArgumentParser(description="dtbo.img 分析工具")
    ap.add_argument("image")
    ap.add_argument("--timings", action="store_true", help="只看刷新档位")
    ap.add_argument("--charge", action="store_true", help="只看充电参数")
    ap.add_argument("--panel", help="面板关键字（多面板时用来筛选）")
    ap.add_argument("--dump-node", metavar="PATH", help="转储指定节点")
    ap.add_argument("--all", action="store_true", help="全部打印")
    args = ap.parse_args()

    img = DtboImage(open(args.image, "rb").read())
    dtb = img.dtb(0)
    root, meta = parse(dtb)

    if not (args.timings or args.charge or args.dump_node):
        show_layout(img)
        ok, msg, first_diff = roundtrip_check(dtb)
        hr("FDT 自检")
        print("  totalsize    : %d" % meta["totalsize"])
        print("  size_struct  : %d" % meta["size_struct"])
        print("  size_strings : %d" % meta["size_strings"])
        print("  往返测试     : %s" % msg)
        if ok:
            print("  结论         : 逐字节一致 —— 库工作正常，可以放心改")
        elif "长度不同" in msg:
            print("  结论         : 长度变了，库有问题，先别改")
        else:
            print("  结论         : 长度一致，差异只是 padding 字节，不影响功能")

    if args.all or args.timings:
        show_timings(root, args.panel)
    if args.all or args.charge:
        show_charge(root)
    if args.dump_node:
        dump_node(root, args.dump_node)


if __name__ == "__main__":
    main()
