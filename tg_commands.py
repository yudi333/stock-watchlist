"""
輪詢 Telegram 有沒有新訊息，收到指定的指令就觸發對應的 GitHub Actions workflow——
機器人目前只會「發」訊息，從來沒有「收」訊息的能力，要讓它聽得懂你在群組打的字，
只能用輪詢的方式（這個網站沒有一直開著的伺服器，沒辦法做到訊息一送出就馬上反應）。

目前支援的指令：
  「重新發」（整句，前後空白會被忽略）——
    - 在 TG_CHAT_ID（DiStocks 每日摘要）那個聊天室打，觸發 daily.yml（整個完整分析重跑一次）
    - 在 TG_CHAT_ID_STOP（已停損/已停利通知）那個聊天室打，觸發 backtest.yml（只重跑回測，
      不用重跑完整分析，比較快）
  其他聊天室、或不認得的文字，一律忽略。

不特別保存「上次讀到哪裡」這種跨次執行的狀態（offset）——每次執行都呼叫 getUpdates 拿到
所有「還沒確認過」的訊息，處理完以後再呼叫一次 getUpdates、帶上這批訊息裡最大的
update_id+1，跟 Telegram 說「這些都看過了，之後不用再給」，這樣不用額外保存/部署任何
檔案就能避免重複觸發。只處理「訊息時間在最近幾分鐘內」的，太舊的一樣會被確認掉（不會
一直卡著讓下次執行又看到），但不會觸發任何動作——避免這支 workflow 萬一中斷一段時間、
恢復執行時，把累積的一堆過期舊指令全部補發一次。

用法（在 GitHub Actions 裡定期執行，見 tg-commands.yml）：
  python tg_commands.py

需要環境變數：
  TG_BOT_TOKEN                  BotFather 給的機器人 token
  TG_CHAT_ID / TG_CHAT_ID_STOP  兩個聊天室各自的 chat id
  GITHUB_TOKEN                  觸發其他 workflow 用（Actions 內建提供，要有 actions: write 權限）
  GITHUB_REPOSITORY             格式 "擁有者/repo 名稱"（Actions 內建提供）
沒設定 TG_BOT_TOKEN 就直接略過，不會讓這次執行失敗。
"""

import os
import sys
import time

import requests

RECENT_SECONDS = 600   # 訊息時間要在這個秒數內才處理（輪詢間隔的好幾倍，留緩衝但不會處理到過期太久的）
COMMAND = "重新發"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def trigger_workflow(repo, token, workflow_file):
    """用 GitHub API 觸發另一個 workflow 的 workflow_dispatch（跟手動按「Run workflow」等效）。
    回傳 True/False 代表有沒有成功送出觸發請求（不是等它跑完）。"""
    try:
        r = requests.post(
            f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            json={"ref": "main"}, timeout=20)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[注意] 觸發 {workflow_file} 失敗（{type(e).__name__}：{e}）")
        return False


def send_ack(token, chat_id, text):
    """收到指令後先回一句「收到了」——實際跑完要好幾分鐘，不然不知道指令有沒有生效。
    失敗也沒關係，不影響指令本身有沒有被觸發。"""
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=20)
    except Exception:
        pass


def main():
    token = os.environ.get("TG_BOT_TOKEN")
    if not token:
        print("沒有設定 TG_BOT_TOKEN，略過。")
        return 0
    chat_targets = {
        os.environ.get("TG_CHAT_ID", ""): ("daily.yml", "每日分析（daily-update）"),
        os.environ.get("TG_CHAT_ID_STOP", ""): ("backtest.yml", "回測（backtest）"),
    }
    chat_targets.pop("", None)   # 兩個環境變數都可能沒設定，空字串不是有效的 chat id，先濾掉
    if not chat_targets:
        print("沒有設定 TG_CHAT_ID / TG_CHAT_ID_STOP，略過。")
        return 0

    gh_token = os.environ.get("GITHUB_TOKEN")
    gh_repo = os.environ.get("GITHUB_REPOSITORY")
    if not gh_token or not gh_repo:
        print("沒有設定 GITHUB_TOKEN / GITHUB_REPOSITORY，無法觸發 workflow，略過。")
        return 0

    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"timeout": 0}, headers=HEADERS, timeout=20)
        r.raise_for_status()
        updates = r.json().get("result", [])
    except Exception as e:
        print(f"[注意] 讀取 Telegram 訊息失敗（{type(e).__name__}），略過這次。")
        return 0

    if not updates:
        print("沒有新訊息。")
        return 0

    now = time.time()
    max_update_id = max(u["update_id"] for u in updates)
    triggered = 0
    for u in updates:
        msg = u.get("message") or u.get("channel_post")
        if not msg:
            continue
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        age = now - msg.get("date", 0)
        if chat_id not in chat_targets:
            continue   # 不是我們在管的兩個聊天室，忽略（不管打了什麼）
        if text != COMMAND:
            continue   # 看得懂這個聊天室，但不是認得的指令
        if age > RECENT_SECONDS:
            print(f"指令「{COMMAND}」太舊了（{age:.0f} 秒前），跳過不處理。")
            continue
        workflow_file, label = chat_targets[chat_id]
        print(f"收到指令「{COMMAND}」，來自「{label}」的聊天室，觸發 {workflow_file}。")
        if trigger_workflow(gh_repo, gh_token, workflow_file):
            triggered += 1
            send_ack(token, chat_id, f"收到「{COMMAND}」，已觸發{label}重新執行，跑完會有新的通知。")
        else:
            send_ack(token, chat_id, f"收到「{COMMAND}」，但觸發{label}失敗了，麻煩自己到 GitHub 檢查。")

    # 不管有沒有真的觸發任何動作，看過的訊息都要跟 Telegram 確認掉，下次才不會又收到同一批
    try:
        requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                    params={"offset": max_update_id + 1, "timeout": 0}, headers=HEADERS, timeout=20)
    except Exception:
        pass   # 確認失敗頂多下次重複看到這批舊訊息，不影響這次已經觸發的動作

    print(f"這次處理了 {len(updates)} 則訊息，觸發了 {triggered} 次。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
