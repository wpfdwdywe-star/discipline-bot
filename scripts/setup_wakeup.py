"""在 cron-job.org 上一键创建定时唤醒任务（给会自动休眠的免费服务器用，比如 instabox）。

用法：
  python scripts/setup_wakeup.py --key 你的cron-job.org API key --url https://你的面板地址/healthz

会创建 5 个任务（北京时间，周一到周五），交易时段一直醒着、中午休息：
  9:20–9:55、10:00–10:55、11:00–11:30、13:00–14:55、15:00–16:55，每 5 分钟访问一次 /healthz
连续两次唤醒失败会发邮件通知你。已经存在同名任务的会跳过，重复运行不会建重复。
API key 在 cron-job.org 登录后的 Settings → API 里生成。
"""
import argparse
import sys
import time

import requests

API = "https://api.cron-job.org/jobs"
E5 = list(range(0, 60, 5))
JOBS = [
    ("纪律机器人·上午① 9:20-9:55", [9], list(range(20, 60, 5))),
    ("纪律机器人·上午② 10:00-10:55", [10], E5),
    ("纪律机器人·上午③ 11:00-11:30", [11], list(range(0, 31, 5))),
    ("纪律机器人·下午 13:00-14:55", [13, 14], E5),
    ("纪律机器人·收盘检查+简报 15:00-16:55", [15, 16], E5),
]


def main() -> None:
    p = argparse.ArgumentParser(description="在 cron-job.org 创建定时唤醒任务")
    p.add_argument("--key", required=True, help="cron-job.org API key")
    p.add_argument("--url", required=True, help="唤醒地址，以 /healthz 结尾")
    args = p.parse_args()
    if not args.url.rstrip("/").endswith("/healthz"):
        sys.exit("--url 应该以 /healthz 结尾（这个地址不需要密码，也不返回任何持仓信息）")

    headers = {"Authorization": f"Bearer {args.key}", "Content-Type": "application/json"}
    r = requests.get(API, headers=headers, timeout=30)
    if r.status_code != 200:
        sys.exit(f"API key 无效或没有权限（HTTP {r.status_code}）：{r.text[:200]}")
    existing = {j["title"] for j in r.json()["jobs"]}

    for title, hours, minutes in JOBS:
        if title in existing:
            print(f"跳过（已存在）：{title}")
            continue
        body = {"job": {
            "url": args.url, "enabled": True, "title": title, "saveResponses": True, "requestTimeout": 60,
            "schedule": {"timezone": "Asia/Shanghai", "expiresAt": 0, "hours": hours, "minutes": minutes,
                         "mdays": [-1], "months": [-1], "wdays": [1, 2, 3, 4, 5]},
            "notification": {"onFailure": True, "onFailureCount": 2, "onSuccess": True, "onDisable": True,
                             "mode": 1},
        }}
        r = requests.put(API, headers=headers, json=body, timeout=30)
        print(f"{'已创建' if r.status_code == 200 else '失败'}：{title}  {r.text[:120]}")
        time.sleep(13)  # cron-job.org 限制每分钟最多创建 5 个任务
    print("\n完成。建议用完后在 cron-job.org 删掉这个 API key，以后需要再生成。")


if __name__ == "__main__":
    main()
