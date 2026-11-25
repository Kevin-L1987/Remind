#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""孩子课程提醒 · 微信推送脚本

支持三类规则：weekly(每周重复) / once(一次) / lunar(农历年度，含闰月策略)
支持多级提前提醒（提前 X 分钟 / 小时 / 天），支持 PushPlus 与 Server酱。

用法：
  python push.py --once --window 6        # 配合定时任务，检查过去 6 分钟内该响的铃
  python push.py --daemon                 # 常驻，每 30 秒检查一次（家里电脑/NAS）
  python push.py --dry-run --once --window 60   # 本地预览，不真发
  python push.py --test                  # 立刻发一条测试消息，验证 token

环境变量：
  PUSH_CHANNEL     pushplus（默认）| serverchan
  PUSHPLUS_TOKEN   PushPlus 的 token
  SERVERCHAN_KEY   Server酱 Turbo 的 SENDKEY
  RULES_PATH       规则文件路径，默认同目录 rules.json
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

try:
    from lunardate import LunarDate
except ImportError:
    sys.exit("缺少依赖，请先执行：pip install lunardate")

TZ = ZoneInfo("Asia/Shanghai")
ROOT = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.environ.get("RULES_PATH") or os.path.join(ROOT, "rules.json")
STATE_PATH = os.path.join(ROOT, "state", "sent.json")
WEEK_CN = "一二三四五六日"


# ---------------------------------------------------------------- 工具
def fmt_lead(m):
    if m < 60:
        return f"{m} 分钟"
    if m < 1440:
        return f"{round(m / 60, 1)} 小时"
    return f"{round(m / 1440, 1)} 天"


def num(v):
    v = float(v)
    return int(v) if v.is_integer() else round(v, 2)


def yuan(v):
    v = float(v)
    return f"¥{v:,.0f}" if abs(v - round(v)) < 0.005 else f"¥{v:,.2f}"


def load_rules():
    with open(RULES_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    rules = cfg.get("rules", cfg if isinstance(cfg, list) else [])
    return [r for r in rules if r.get("enabled", True)]


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sent": {}}


def save_state(st):
    cutoff = (datetime.now(TZ) - timedelta(days=7)).timestamp()
    st["sent"] = {k: v for k, v in st.get("sent", {}).items() if v > cutoff}
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


# ---------------------------------------------------------------- 农历
def lunar_to_solar(year, month, day, policy):
    """该农历年某月某日对应的公历日期列表（ISO 字符串）"""
    leap = LunarDate.leapMonthForYear(year) or 0

    def solar(m, is_leap, d):
        try:
            s = LunarDate(year, m, d, is_leap).toSolarDate()
            back = LunarDate.fromSolarDate(s.year, s.month, s.day)
            if back.month == m and back.day == d and bool(back.isLeapMonth) == is_leap:
                return s.isoformat()
        except Exception:
            pass
        return None

    def pick(m, is_leap):
        # 小月没有「三十」时，回退到该月最后一天
        for d in range(min(day, 30), 0, -1):
            v = solar(m, is_leap, d)
            if v:
                return v
        return None

    normal = pick(month, False)
    if policy == "preferLeap":
        v = pick(month, True) if leap == month else None
        return [v or normal] if (v or normal) else []
    if policy == "both":
        v = pick(month, True) if leap == month else None
        return list(dict.fromkeys([x for x in [normal, v] if x]))
    return [normal] if normal else []


def solar_dates_for(rule, day):
    """判断公历 day 是否是这条农历年度规则该响的日子"""
    out = []
    lm = rule.get("lunarMonth", rule.get("lunar_month", 0))
    ld = rule.get("lunarDay", rule.get("lunar_day", 0))
    lp = rule.get("leapPolicy", rule.get("leap_policy", "normal"))
    if not lm or not ld:
        return out
    # 农历腊月常落在公历次年 1-2 月，所以要同时查当年和上一年农历年
    for ly in (day.year, day.year - 1):
        for s in lunar_to_solar(ly, lm, ld, lp):
            if s == day.isoformat():
                out.append(ly)
                break
    return out


# ---------------------------------------------------------------- 触发计算
def occurrences(rule, day):
    """该公历日的事件时刻列表（带时区的 datetime）"""
    H, M = map(int, rule.get("time", "19:00").split(":"))
    ev = datetime(day.year, day.month, day.day, H, M, tzinfo=TZ)
    typ = rule.get("type", "weekly")
    if typ == "weekly":
        if day.weekday() in rule.get("days", []):
            start, until = rule.get("start"), rule.get("until")
            if start and day.isoformat() < start:
                return []
            if until and day.isoformat() > until:
                return []
            return [ev]
    elif typ == "once":
        if day.isoformat() == rule.get("date"):
            return [ev]
    elif typ == "lunar":
        if solar_dates_for(rule, day):
            return [ev]
    return []


def collect(rules, w_start, w_end):
    """窗口内应触发的提醒"""
    out = []
    max_lead = max((max(r.get("leads") or [0]) for r in rules), default=0)
    d = (w_start - timedelta(minutes=max_lead) - timedelta(days=1)).date()
    last = (w_end + timedelta(days=1)).date()
    while d <= last:
        for r in rules:
            for ev in occurrences(r, d):
                for lead in (r.get("leads") or []):
                    trg = ev - timedelta(minutes=lead)
                    if w_start <= trg <= w_end:
                        key = hashlib.md5(
                            f"{r.get('id', '')}|{trg.isoformat()}|{lead}".encode()
                        ).hexdigest()
                        out.append({"key": key, "rule": r, "event": ev, "trigger": trg, "lead": lead})
        d += timedelta(days=1)
    out.sort(key=lambda x: x["trigger"])
    return out


def used_lessons(rule, today):
    """返回 (已用节数, 剩余节数, 单价)；未设置课时返回 None"""
    total = float(rule.get("totalLessons") or 0)
    if total <= 0:
        return None
    per = float(rule.get("lessonsPerSession") or 1)
    unit = float(rule.get("feePerLesson") or 0)
    manual = rule.get("usedLessons")
    if manual not in (None, "", 0, "0"):
        used = float(manual)
    else:
        start = rule.get("start") or (today - timedelta(days=180)).isoformat()
        d, cnt = date.fromisoformat(start), 0
        while d <= today:
            if occurrences(rule, d):
                cnt += 1
            d += timedelta(days=1)
        used = cnt * per
    used = min(used, total)
    return used, max(0.0, total - used), unit


def build_message(item):
    r, ev, lead = item["rule"], item["event"], item["lead"]
    child, title = r.get("child", ""), r.get("title", "")
    head = f"【提醒】{child} · {title} {fmt_lead(lead)}后开始"
    lines = [f"### {child} · {title}", "",
             f"- **开始时间**：{ev.strftime('%Y-%m-%d %H:%M')}（周{WEEK_CN[ev.weekday()]}）",
             f"- **本次提醒**：提前 {fmt_lead(lead)}",
             f"- **预计时长**：{r.get('duration', 60)} 分钟"]
    if r.get("location"):
        lines.append(f"- **地点**：{r['location']}")
    if r.get("note"):
        lines.append(f"- **备注**：{r['note']}")
    tail = used_lessons(r, ev.date())
    if tail:
        used, remain, unit = tail
        lines.append(f"- **剩余课时**：{num(remain)} 节 / 共 {num(r['totalLessons'])} 节"
                     + (f"，剩余费用 {yuan(remain * unit)}" if unit else ""))
    if r.get("type") == "lunar":
        lines += ["", f"> 农历 {r['lunarMonth']} 月 {r['lunarDay']} 日 · 按农历逐年换算"]
    lines += ["", "> 课程提醒规划器自动发送"]
    return head, "\n".join(lines)


# ---------------------------------------------------------------- 推送
def push(title, content):
    channel = os.environ.get("PUSH_CHANNEL", "pushplus").lower()
    if channel == "serverchan":
        key = os.environ.get("SERVERCHAN_KEY")
        if not key:
            raise RuntimeError("缺少 SERVERCHAN_KEY")
        url = f"https://sctapi.ftqq.com/{key}.send"
        data = urllib.parse.urlencode({"title": title, "desp": content}).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    else:
        token = os.environ.get("PUSHPLUS_TOKEN")
        if not token:
            raise RuntimeError("缺少 PUSHPLUS_TOKEN")
        url = "https://www.pushplus.plus/send"
        data = json.dumps({"token": token, "title": title,
                           "content": content, "template": "markdown"},
                          ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", "replace")[:300]


def run(args):
    rules = load_rules()
    now = datetime.now(TZ)
    w_end = now
    w_start = now - timedelta(seconds=args.window * 60 if args.once else 70)
    items = collect(rules, w_start, w_end)
    st = load_state()
    sent = 0
    for it in items:
        if it["key"] in st.get("sent", {}):
            continue
        title, content = build_message(it)
        ts = it["trigger"].strftime("%Y-%m-%d %H:%M")
        if args.dry_run:
            print(f"[预览] {ts} | {title}\n{content}\n{'-' * 50}")
        else:
            try:
                ret = push(title, content)
                print(f"[已推送] {ts} | {title} | {ret}")
            except Exception as e:
                print(f"[推送失败] {ts} | {title} | {e}", file=sys.stderr)
                continue
        st.setdefault("sent", {})[it["key"]] = now.timestamp()
        sent += 1
    if not args.dry_run:
        save_state(st)
    print(f"检查完成：{len(rules)} 条规则，窗口内 {len(items)} 个触发点，本次处理 {sent} 条")


def main():
    ap = argparse.ArgumentParser(description="孩子课程提醒 · 微信推送")
    ap.add_argument("--once", action="store_true", help="单次检查（配合定时任务）")
    ap.add_argument("--window", type=int, default=6, help="--once 时回看多少分钟，默认 6")
    ap.add_argument("--daemon", action="store_true", help="常驻循环，每 30 秒检查")
    ap.add_argument("--dry-run", action="store_true", help="只打印不推送")
    ap.add_argument("--test", action="store_true", help="立刻发一条测试消息")
    args = ap.parse_args()

    if args.test:
        print(push("【测试】课程提醒已接通", "### 推送通道配置成功\n\n"
                   "接下来到点会自动提醒孩子上课。\n\n> 课程提醒规划器"))
        return
    if args.daemon:
        print(f"常驻模式启动，规则文件：{RULES_PATH}，Ctrl+C 退出")
        while True:
            try:
                run(args)
            except Exception as e:
                print(f"[异常] {e}", file=sys.stderr)
            time.sleep(30)
    else:
        run(args)


if __name__ == "__main__":
    main()
