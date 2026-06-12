#!/usr/bin/env python3
"""Telegram 告警一键自检（P0 实时告警的本地 onboarding 工具）。

用途：拿到机器人 token 后，自动获取你的 chat_id 并发一条测试告警，验证打通。
**token/chat_id 只经环境变量读取，绝不写入任何文件。**

用法：
    # 1) 先在 Telegram 给你的 bot 发任意一条消息（必须先点 Start）
    # 2) 运行（在你自己的机器上，网络能正常访问 Telegram）：
    TELEGRAM_BOT_TOKEN='你的token' python scripts/telegram_setup.py

    # 若已知 chat_id，可跳过自动获取、直接发测试：
    TELEGRAM_BOT_TOKEN='...' TELEGRAM_CHAT_ID='123456789' python scripts/telegram_setup.py

成功后，把得到的 chat_id 设进环境变量再启动服务即可让告警生效：
    TELEGRAM_BOT_TOKEN='...' TELEGRAM_CHAT_ID='...' \
        .venv/bin/python -m uvicorn scanner.app:app
"""

from __future__ import annotations

import os
import sys

import httpx


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("❌ 未设置 TELEGRAM_BOT_TOKEN 环境变量。")
        return 2
    base = f"https://api.telegram.org/bot{token}"

    me = httpx.get(f"{base}/getMe", timeout=15).json()
    if not me.get("ok"):
        print("❌ token 无效：", me)
        return 2
    print(f"✅ Bot 有效：@{me['result'].get('username')}")

    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        upd = httpx.get(f"{base}/getUpdates", params={"timeout": 5}, timeout=20).json()
        chats = []
        for u in upd.get("result", []):
            msg = u.get("message") or u.get("channel_post") or {}
            ch = msg.get("chat") or {}
            if ch.get("id") is not None:
                chats.append((ch["id"], ch.get("type"), ch.get("first_name") or ch.get("title")))
        if not chats:
            print(
                "⚠️ 没拿到任何消息。请先在 Telegram 打开你的 bot、点 Start、发一条 'hi'，"
                "再重跑本脚本；或用 @userinfobot 查到数字 chat_id 后用 TELEGRAM_CHAT_ID 传入。"
            )
            return 1
        print("找到以下会话：")
        for cid, ctype, name in chats:
            print(f"  chat_id={cid}  type={ctype}  name={name}")
        chat_id = str(chats[-1][0])
        print(f"→ 使用最近的 chat_id={chat_id}")

    text = (
        "✅ 预测市场套利扫描器：Telegram 告警已打通！\n"
        "今后检测到合格的跨平台套利信号会推送到这里。\n"
        "⚠️ 下单前请务必核对两市场是否为同一事件。"
    )
    r = httpx.post(f"{base}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=15)
    if r.status_code == 200 and r.json().get("ok"):
        print(f"✅ 测试告警已发送到 chat_id={chat_id}。把它设进 TELEGRAM_CHAT_ID 即可。")
        return 0
    print("❌ 发送失败：", r.status_code, r.text[:200])
    if r.status_code == 403:
        print("提示：403 通常是你还没对该 bot 点过 Start——先在 Telegram 里 Start 这个 bot。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
