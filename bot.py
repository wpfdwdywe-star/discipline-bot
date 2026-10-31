"""交易纪律机器人 · 命令行（日常用 App 就行：双击 启动纪律机器人.command）。

用法：
  python bot.py add 600519               建仓：自动找平台支撑位，选一个锁定
  python bot.py add 600519 --support 12.5 建仓：手动指定支撑位（新买入必须过买前体检）
  python bot.py checkup 600519            买前体检
  python bot.py raise 600519 13.2         上移支撑位（只能上移）
  python bot.py list                      查看持仓和状态
  python bot.py sold 600519 --price 12.3  报告已卖出
  python bot.py close                     收盘检查
  python bot.py intraday                  观察日 14:40 检查
  python bot.py report [--month 2026-10]  月度纪律报告
  python bot.py daemon                    常驻后台，到点自动检查（不带界面）
"""
import argparse
import sys

import core
from core import Refused
from notify import push
from store import SELL, WATCH


def cmd_add(args) -> None:
    if args.support:
        h = core.add_holding(args.code, args.support, existing=args.existing)
    else:
        c = core.candidate_platforms(args.code)
        if not c["platforms"]:
            raise Refused("现价下方没找到 ≥3 天、振幅 ≤10% 的横盘平台，请用 --support 手动指定。")
        print(f"{c['name']}({c['code']}) 最新收盘 {c['price']}，找到这些平台：")
        for i, p in enumerate(c["platforms"], 1):
            print(f"  [{i}] {p['level']} {p['start']} ~ {p['end']} 共{p['days']}天  "
                  f"区间 {p['low']}-{p['high']}  支撑位 {p['support']}")
        pick = input("选哪个作为支撑位？输入编号：").strip()
        if not pick.isdigit() or not 1 <= int(pick) <= len(c["platforms"]):
            raise Refused("没选，取消。")
        p = c["platforms"][int(pick) - 1]
        print(f"\n确认锁定：支撑位 {p['support']}（{p['level']}）")
        print("锁定后只能上移，不能下移。收盘跌破 → 观察一天 → 14:40 没有收回就清仓。")
        if input("输入 yes 确认：").strip().lower() != "yes":
            raise Refused("取消。")
        h = core.add_holding(args.code, p["support"], p["level"], f"{p['start']}~{p['end']}({p['days']}天)",
                             existing=args.existing)
    print(f"已锁定 {h['name']}({h['code']}) 支撑位 {h['support']}")


def cmd_list(args) -> None:
    hs = core.holdings()
    if not hs:
        print("暂无持仓。")
    for h in hs:
        extra = f"  跌破日 {h['watch_date']}" if h["status"] == WATCH else ""
        extra = f"  信号日 {h['sell_date']}" if h["status"] == SELL else extra
        print(f"{h['name']}({h['code']})".ljust(16) + f" 支撑 {h['support']:<8} {h['level']:<6} [{h['status']}]{extra}")


def main() -> None:
    p = argparse.ArgumentParser(description="交易纪律机器人")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("code"); a.add_argument("--support", type=float)
    a.add_argument("--existing", action="store_true", help="补录已经持有的票（体检不建议也不拦）")
    c = sub.add_parser("checkup"); c.add_argument("code")
    r = sub.add_parser("raise"); r.add_argument("code"); r.add_argument("price", type=float)
    sub.add_parser("list")
    s = sub.add_parser("sold"); s.add_argument("code"); s.add_argument("--price", type=float)
    sub.add_parser("close")
    sub.add_parser("intraday")
    rp = sub.add_parser("report"); rp.add_argument("--month")
    sub.add_parser("daemon")
    args = p.parse_args()
    try:
        match args.cmd:
            case "add": cmd_add(args)
            case "raise":
                h = core.raise_support(args.code, args.price)
                print(f"{h['name']}({h['code']}) 支撑位上移到 {h['support']}")
            case "list": cmd_list(args)
            case "checkup": print(core.pre_buy_checkup(args.code)["report"])
            case "sold": print(core.record_sold(args.code, args.price))
            case "close": core.check_close()
            case "intraday": core.check_intraday()
            case "report": push(f"{args.month or '本月'} 纪律报告", core.build_report(args.month))
            case "daemon":
                print("纪律机器人已启动（北京时间 14:40 观察日检查，15:30 收盘检查）")
                core.Scheduler().loop()
    except Refused as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
