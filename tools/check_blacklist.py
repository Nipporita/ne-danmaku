# -*- coding: utf-8 -*-
"""黑名单误伤体检：看看当前生效的规则会不会把「正常内容」也标成 blocked。

为什么需要它：`blacklist_extra_files` 引进来的是通用文本审核词库（第三方开源项目），
它们是给论坛/评论用的，直接拿来卡弹幕必然误伤 —— 例如某个词库里有 `早苗`
（本来是想匹配"高市早苗"），但在东方活动上"东风谷早苗"才是常态，结果正常弹幕被吞。

这个脚本按**后端实际生效的规则集**（主文件 + 所有额外词表，走同一套
BlacklistService 逻辑）逐条测试保护语料，并指出"这条来自哪个文件"。

用法（在 ne-danmaku 仓库根目录下）：
    python tools/check_blacklist.py
    python tools/check_blacklist.py --smww <东方角色名清单.json>   # 默认自动找 danmaku-app 那份
    python tools/check_blacklist.py --config config.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import regex  # noqa: E402

from nekocast_danmaku.config import load_config  # noqa: E402
from nekocast_danmaku.danmaku.models import BlacklistService  # noqa: E402

DEFAULT_SMWW = pathlib.Path(r"D:\fantasyguide\danmaku-app\danmaku-frontend\SMWW.json")

# 游戏侧的控制词与元指令：被过滤掉不会影响游戏（前端不关心 blocked），
# 但大屏上看不到主持人指令会很奇怪，所以也算误伤
CONTROL_WORDS = ["开始报名", "开始游戏", "开始打分", "结束打分", "下一回合", "结束游戏",
                 "/set 5", "/remove 2", "/clear 3"]

COMMON_DANMAKU = ["好耶", "哈哈哈哈哈", "2333", "太强了", "强强", "加油", "东方", "幻想乡",
                  "这题我会", "我觉得是主持人", "整活", "草", "？", "???", "！", "笑死",
                  "绷不住了", "绝了", "前排", "来了来了", "支持", "好活", "有能", "天才",
                  "典", "急", "乐", "6", "0"]


def build_corpus(smww: pathlib.Path) -> list[str]:
    corpus = list(CONTROL_WORDS) + [str(i) for i in range(10)] + list(COMMON_DANMAKU)
    if smww.exists():
        try:
            corpus += list(json.loads(smww.read_text(encoding="utf-8")).keys())
            print("角色名清单：%s（%d 个）" % (smww, len(json.loads(smww.read_text(encoding='utf-8')))))
        except Exception as exc:
            print("!! 读角色名清单失败：%s" % exc)
    else:
        print("!! 没找到角色名清单（%s），这一路体检跳过" % smww)
    return sorted({c for c in corpus if c})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(REPO_ROOT / "config.json"))
    ap.add_argument("--smww", default=str(DEFAULT_SMWW))
    ap.add_argument("--limit", type=int, default=60, help="每类最多打印多少条")
    args = ap.parse_args()

    config = load_config(pathlib.Path(args.config))
    dc = config.danmaku

    service = BlacklistService(extra_pattern_files=dc.resolved_blacklist_extra_files)
    service.reload(dc.resolved_blacklist_file, dc.resolved_forbidden_users_file)

    main_count = len(service._pattern_in_file)
    total = len(service._patterns)
    print("\n主黑名单：%s（%d 条正则）" % (dc.resolved_blacklist_file, main_count))
    print("额外词表：%d 份" % len(dc.resolved_blacklist_extra_files))
    for p in dc.resolved_blacklist_extra_files:
        print("   %s%s" % (p, "" if pathlib.Path(p).exists() else "   <- 不存在！"))
    print("生效规则合计：%d 条" % total)

    corpus = build_corpus(pathlib.Path(args.smww))
    print("\n保护语料 %d 条（控制词 %d / 数字 10 / 常见弹幕 %d / 角色名若干）"
          % (len(corpus), len(CONTROL_WORDS), len(COMMON_DANMAKU)))

    hits: list[tuple[str, str, str]] = []      # (规则, 命中的语料, 来源文件)
    for pattern in service._patterns:
        source = service._pattern_sources.get(pattern.pattern, "?")
        for safe in corpus:
            if pattern.search(safe):
                hits.append((pattern.pattern, safe, source))
                break

    print("\n会被误伤的规则：%d 条" % len(hits))
    by_source: dict[str, int] = {}
    for _, _, source in hits:
        by_source[source] = by_source.get(source, 0) + 1
    for source, count in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print("   %-72s %d 条" % (pathlib.Path(source).name, count))

    print("\n明细（最多 %d 条）：" % args.limit)
    for pattern, safe, source in hits[:args.limit]:
        print("   %-24s 会命中 %-12s ← %s" % (repr(pattern), safe, pathlib.Path(source).name))
    if len(hits) > args.limit:
        print("   ...（还有 %d 条）" % (len(hits) - args.limit))

    # 性能：每条弹幕都要过一遍全部规则
    samples = ["我觉得是主持人", "哈哈哈哈哈", "整活！", "3", "好耶", "东方project", "开始报名"]
    t0 = time.perf_counter()
    for _ in range(100):
        for s in samples:
            for pattern in service._patterns:
                pattern.search(s)
    per = (time.perf_counter() - t0) / (100 * len(samples)) * 1000
    print("\n性能：%d 条规则 × 单条弹幕 平均 %.2f ms" % (total, per))
    print("\n提示：误伤只影响大屏显示（被标 blocked 的消息不渲染），游戏计票/答案不受影响；")
    print("      但主持人指令、正常弹幕看不见会很难受，建议把太吵的整份词表从")
    print("      config.json 的 blacklist_extra_files 里去掉。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
