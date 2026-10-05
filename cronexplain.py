"""cronexplain - 用人话解释 cron 表达式，并算出接下来几次执行时间。

纯标准库，无依赖。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta

VERSION = "0.1.0"

SHORTCUTS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

MONTH_NAMES = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
DOW_NAMES = {
    "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6, "SUN": 7,
}

FIELD_SPECS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day_of_month", 1, 31),
    ("month", 1, 12),
    ("day_of_week", 0, 7),
)

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
MONTH_CN = [f"{m}月" for m in range(1, 13)]


class CronError(ValueError):
    pass


def _replace_names(token: str, names: dict) -> str:
    def sub(m):
        name = m.group(0).upper()
        if name not in names:
            raise CronError(f"未知的名称：{m.group(0)}")
        return str(names[name])

    return re.sub(r"[A-Za-z]+", sub, token)


def parse_field(token: str, lo: int, hi: int, names: dict | None = None) -> set[int]:
    """把单个 cron 字段解析成取值集合。"""
    if names:
        token = _replace_names(token, names)
    token = token.strip()
    if not token:
        raise CronError("字段不能为空")
    values: set[int] = set()
    for part in token.split(","):
        part = part.strip()
        m = re.fullmatch(r"(\*|\d+)(?:-(\d+))?(?:/(\d+))?", part)
        if not m:
            raise CronError(f"无法解析的字段片段：{part!r}")
        base, end, step_s = m.groups()
        step = int(step_s) if step_s else 1
        if step <= 0:
            raise CronError(f"步长必须为正整数：{part!r}")
        if base == "*":
            start, stop = lo, hi
        elif end is not None:
            start, stop = int(base), int(end)
        else:
            start = stop = int(base)
        if start < lo or stop > hi or start > stop:
            raise CronError(f"取值超出范围（{lo}-{hi}）：{part!r}")
        values.update(range(start, stop + 1, step))
    return values


class CronExpr:
    def __init__(self, expr: str):
        expr = expr.strip()
        if expr.startswith("@"):
            key = expr.split()[0].lower()
            if key not in SHORTCUTS:
                raise CronError(f"未知的快捷写法：{expr!r}（支持：{', '.join(sorted(SHORTCUTS))}）")
            expr = SHORTCUTS[key]
        fields = expr.split()
        if len(fields) != 5:
            raise CronError(f"cron 表达式需要 5 个字段（分 时 日 月 周），实际 {len(fields)} 个：{expr!r}")
        self.raw = expr
        self.minute = parse_field(fields[0], 0, 59)
        self.hour = parse_field(fields[1], 0, 23)
        self.dom = parse_field(fields[2], 1, 31)
        self.month = parse_field(fields[3], 1, 12, MONTH_NAMES)
        dow = parse_field(fields[4], 0, 7, DOW_NAMES)
        # cron 里 0 和 7 都表示周日，统一成 0-6（周一=0 … 周日=6 按 Python 习惯）
        self.dow = {6 if v in (0, 7) else v - 1 for v in dow}
        # 记录"是否受限"（是否为 *），用于 日/周 的 OR 语义
        self.dom_restricted = fields[2].strip() != "*"
        self.dow_restricted = fields[4].strip() != "*"
        self.fields = fields

    def matches(self, dt: datetime) -> bool:
        if dt.minute not in self.minute:
            return False
        if dt.hour not in self.hour:
            return False
        if dt.month not in self.month:
            return False
        dom_ok = dt.day in self.dom
        dow_ok = dt.weekday() in self.dow
        if self.dom_restricted and self.dow_restricted:
            day_ok = dom_ok or dow_ok  # 真实 cron 语义：两者都受限时是 OR
        elif self.dom_restricted:
            day_ok = dom_ok
        elif self.dow_restricted:
            day_ok = dow_ok
        else:
            day_ok = True
        return day_ok

    def next_runs(self, start: datetime, count: int = 5, limit_days: int = 1461):
        """逐分钟推进找下 count 个执行时刻（不含 start 本身）。

        算法：从 start 的下一分钟开始逐分钟检查 matches()；
        若整月都不在 month 集合里，直接跳到下月 1 日 0 点（否则像
        "0 0 29 2 *" 这种四年一次的表达式要空转数百万分钟）。
        最多向前搜索 limit_days 天（默认 4 年，覆盖闰日周期）。
        """
        runs = []
        cursor = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
        deadline = cursor + timedelta(days=limit_days)
        while cursor <= deadline and len(runs) < count:
            if cursor.month not in self.month:
                # 跳到下个月 1 日 0:00
                if cursor.month == 12:
                    cursor = cursor.replace(year=cursor.year + 1, month=1, day=1, hour=0, minute=0)
                else:
                    cursor = cursor.replace(month=cursor.month + 1, day=1, hour=0, minute=0)
                continue
            if self.matches(cursor):
                runs.append(cursor)
            cursor += timedelta(minutes=1)
        return runs


def explain(expr: CronExpr) -> str:
    # 频率部分（先说"多久一次"）
    dom_s, dow_s, mon_s = sorted(expr.dom), sorted(expr.dow), sorted(expr.month)
    mon_note = "" if mon_s == list(range(1, 13)) else f"，仅在{', '.join(MONTH_CN[m-1] for m in mon_s)}"
    if expr.dom_restricted and expr.dow_restricted:
        freq = (
            f"每月第{', '.join(map(str, dom_s))}天或"
            + "、".join(WEEKDAY_CN[d] for d in dow_s)
            + "（满足其一即执行）"
        )
    elif expr.dom_restricted:
        if len(dom_s) == 1 and len(mon_s) == 1:
            freq = f"每年{mon_s[0]}月{dom_s[0]}日"
        elif len(dom_s) == 1:
            freq = f"每月{dom_s[0]}号{mon_note}"
        else:
            freq = f"每月第{', '.join(map(str, dom_s))}天{mon_note}"
    elif expr.dow_restricted:
        dow_set = set(dow_s)
        if dow_set == {0, 1, 2, 3, 4}:
            freq = f"每个工作日{mon_note}"
        elif dow_set == {5, 6}:
            freq = f"每周末{mon_note}"
        elif dow_set == set(range(0, 7)):
            freq = f"每天{mon_note}"
        else:
            freq = "每" + "、".join(WEEKDAY_CN[d] for d in dow_s) + mon_note
    else:
        freq = f"每天{mon_note}"
    # 时间部分（再说"几点几分"）
    if expr.minute == set(range(0, 60)) and expr.hour == set(range(0, 24)):
        moment = "每分钟"
    elif expr.minute == set(range(0, 60)):
        moment = "整点"
    elif expr.hour == set(range(0, 24)):
        mins = sorted(expr.minute)
        if len(mins) == 1:
            moment = f"每小时的 {mins[0]:02d} 分"
        else:
            moment = f"每小时的第 {', '.join(map(str, mins))} 分钟"
    else:
        times = [f"{h:02d}:{mi:02d}" for h in sorted(expr.hour) for mi in sorted(expr.minute)]
        moment = "、".join(times)
    if moment == "每分钟":
        return f"{freq}每分钟执行"
    return f"{freq} {moment} 执行"


def parse_start(s: str | None) -> datetime:
    if not s:
        return datetime.now()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%H:%M"):
        try:
            dt = datetime.strptime(s.strip(), fmt)
            if fmt == "%H:%M":
                now = datetime.now()
                dt = dt.replace(year=now.year, month=now.month, day=now.day)
            return dt
        except ValueError:
            continue
    raise CronError(f"无法解析起始时间：{s!r}（支持 YYYY-MM-DD HH:MM 等格式）")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cronexplain", description="用人话解释 cron 表达式，并列出接下来几次执行时间。")
    ap.add_argument("expr", help='cron 表达式，如 "0 9 * * 1-5"，或 @daily 等快捷写法')
    ap.add_argument("--from", dest="from_", default=None, help="从该时间起算（默认现在），如 \"2026-10-05 12:00\"")
    ap.add_argument("-n", "--count", type=int, default=5, help="列出接下来几次（默认 5）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = ap.parse_args(argv)

    try:
        expr = CronExpr(args.expr)
        start = parse_start(args.from_)
    except CronError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    runs = expr.next_runs(start, count=args.count)
    desc = explain(expr)

    if args.json:
        print(json.dumps({
            "expr": expr.raw,
            "explanation": desc,
            "from": start.strftime("%Y-%m-%d %H:%M"),
            "next_runs": [r.strftime("%Y-%m-%d %H:%M") + " " + WEEKDAY_CN[r.weekday()] for r in runs],
        }, ensure_ascii=False, indent=2))
    else:
        print(f"表达式：{expr.raw}")
        print(f"含义：{desc}")
        print(f"从 {start.strftime('%Y-%m-%d %H:%M')} 起，接下来 {len(runs)} 次执行：")
        for r in runs:
            print(f"  {r.strftime('%Y-%m-%d %H:%M')} {WEEKDAY_CN[r.weekday()]}")
        if len(runs) < args.count:
            print("（搜索 4 年内没有更多执行时间）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
