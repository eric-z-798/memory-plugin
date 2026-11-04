"""共享看板：三家 AI 的共同记忆。

三家彼此没有记忆，所以群里的共识、分工和进度全部记在看板上，由工具维护：
- 每次轮到某位成员发言，工具把看板交给它；
- 成员在回复末尾附"状态更新"，工具解析后更新看板；
- 决议和任务分配在全体在群 AI 成员都同意后自动生效；
- 群主随时可以提意见（条目退回讨论、三家要先回应），也可以直接确认或否决。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, List, Optional, Tuple

from .common import clip, fmt_time, name_of, normalize_member, now

DECISION_STATUS = {"discussing": "讨论中", "effective": "已生效", "rejected": "已否决"}
TASK_STATUS = {
    "proposed": "提议中",
    "todo": "待办",
    "doing": "进行中",
    "done": "已完成",
    "blocked": "阻塞",
    "cancelled": "已取消",
}
OPEN_TASK = {"proposed"}
ACTIVE_TASK = {"todo", "doing", "blocked"}
CHANGELOG_LIMIT = 300

_VOTE_WORDS = {
    "agree": "agree", "yes": "agree", "approve": "agree", "同意": "agree", "赞成": "agree",
    "支持": "agree", "+1": "agree", "✓": "agree", "✔": "agree",
    "disagree": "disagree", "no": "disagree", "reject": "disagree", "oppose": "disagree",
    "反对": "disagree", "不同意": "disagree", "-1": "disagree", "✗": "disagree", "×": "disagree",
}
_TASK_WORDS = {
    "doing": "doing", "in_progress": "doing", "in progress": "doing", "started": "doing",
    "进行中": "doing", "开始": "doing",
    "done": "done", "completed": "done", "complete": "done", "finished": "done",
    "已完成": "done", "完成": "done",
    "blocked": "blocked", "阻塞": "blocked", "受阻": "blocked", "卡住": "blocked",
}


# ---------------------------------------------------------------- 基本结构

def new_board(goal: str = "") -> dict:
    return {
        "goal": goal,
        "decisions": [],
        "tasks": [],
        "need_user": [],
        "summary": "",
        "summary_upto": 0,
        "changelog": [],
        "counters": {"D": 0, "T": 0, "N": 0},
    }


def upgrade(board: dict) -> dict:
    """补齐旧版本看板缺少的字段。"""
    fresh = new_board()
    for key, value in fresh.items():
        board.setdefault(key, value)
    for key, value in fresh["counters"].items():
        board["counters"].setdefault(key, value)
    return board


def _next_id(board: dict, prefix: str) -> str:
    board["counters"][prefix] = board["counters"].get(prefix, 0) + 1
    return "%s-%03d" % (prefix, board["counters"][prefix])


def log(board: dict, who: str, text: str) -> None:
    board["changelog"].append({"ts": now(), "who": who, "text": clip(text, 300)})
    if len(board["changelog"]) > CHANGELOG_LIMIT:
        del board["changelog"][:-CHANGELOG_LIMIT]


_ID_RE = re.compile(r"^\s*([DTN])\s*[-_]?\s*0*(\d{1,4})\s*$", re.I)


def normalize_id(item_id: Any) -> Optional[str]:
    """D-1、d001、D_001 都认作 D-001。"""
    if not isinstance(item_id, str):
        return None
    m = _ID_RE.match(item_id)
    if not m:
        return None
    return "%s-%03d" % (m.group(1).upper(), int(m.group(2)))


def find_item(board: dict, item_id: Any) -> Tuple[Optional[str], Optional[dict]]:
    key = normalize_id(item_id)
    if not key:
        return None, None
    for item in board["decisions"]:
        if item["id"] == key:
            return "decision", item
    for item in board["tasks"]:
        if item["id"] == key:
            return "task", item
    return None, None


def is_open(kind: str, item: dict) -> bool:
    """条目是否还在等待表态（可以投票）。"""
    return item["status"] == "discussing" if kind == "decision" else item["status"] in OPEN_TASK


def _norm(text: str) -> str:
    return re.sub(r"[\s，,。.!！?？;；:：、\"'“”‘’（）()]+", "", text or "").lower()


def _vote(member: str, vote: str, reason: str = "") -> dict:
    return {"vote": vote, "reason": clip(reason, 300), "ts": now()}


def parse_vote(value: Any) -> Optional[str]:
    if value is True:
        return "agree"
    if value is False:
        return "disagree"
    if not isinstance(value, str):
        return None
    return _VOTE_WORDS.get(value.strip().lower())


def parse_task_status(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return _TASK_WORDS.get(value.strip().lower())


# ---------------------------------------------------------------- 共识

def check_consensus(board: dict, kind: str, item: dict, members: List[str]) -> Optional[str]:
    """全体在群 AI 成员都同意，条目就生效。返回一条说明，没生效返回 None。"""
    if not members or not is_open(kind, item):
        return None
    if not all(item["votes"].get(m, {}).get("vote") == "agree" for m in members):
        return None
    item["status"] = "effective" if kind == "decision" else "todo"
    item["updated"] = now()
    for note in item.get("user_notes", []):
        note["pending"] = False
    text = "%s 全员同意，%s" % (item["id"], "已生效" if kind == "decision" else "分工确认，进入待办")
    log(board, "system", text)
    return text


def recheck_all(board: dict, members: List[str]) -> List[str]:
    """成员变动后重新检查一遍（例如移出某位成员后，剩下的人已经全部同意）。"""
    results = []
    for item in board["decisions"]:
        msg = check_consensus(board, "decision", item, members)
        if msg:
            results.append(msg)
    for item in board["tasks"]:
        msg = check_consensus(board, "task", item, members)
        if msg:
            results.append(msg)
    return results


# ---------------------------------------------------------------- 成员的状态更新

def _iter_votes(raw: Any) -> Iterable[Tuple[str, str, str]]:
    if isinstance(raw, dict):  # {"D-001": "agree"}
        raw = [{"id": k, "vote": v} for k, v in raw.items()]
    if not isinstance(raw, list):
        return
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        vid = entry.get("id") or entry.get("item")
        vote = entry.get("vote")
        if isinstance(vote, dict):  # {"D-001": {"vote": "disagree", "reason": "..."}}
            entry, vote = vote, vote.get("vote")
        vote = parse_vote(vote)
        if isinstance(vid, str) and vote:
            yield vid, vote, str(entry.get("reason") or "").strip()


def _iter_texts(raw: Any) -> Iterable[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return
    for entry in raw:
        if isinstance(entry, dict):
            entry = entry.get("text") or entry.get("title") or ""
        if isinstance(entry, str) and entry.strip():
            yield entry.strip()


def _iter_tasks(raw: Any) -> Iterable[dict]:
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, list):
        return
    for entry in raw:
        if isinstance(entry, str):
            entry = {"title": entry}
        if isinstance(entry, dict) and str(entry.get("title") or "").strip():
            yield entry


def _find_similar(items: List[dict], field: str, text: str) -> Optional[dict]:
    key = _norm(text)
    if not key:
        return None
    for item in items:
        if item["status"] in ("rejected", "cancelled"):
            continue
        if _norm(item[field]) == key:
            return item
    return None


def _single_lines(value):
    if isinstance(value, str):
        return value.replace("\r", " ").replace("\n", " ").replace("【群主意见·工具标注】", "（条目内身份字样）")
    if isinstance(value, list):
        return [_single_lines(v) for v in value]
    if isinstance(value, dict):
        return {k: _single_lines(v) for k, v in value.items()}
    return value


def apply_status(board: dict, member: str, status: Any, members: List[str], seq: Optional[int] = None) -> List[str]:
    """把成员的状态更新写进看板，返回给界面显示的简短效果列表。

    seq 是这次发言在聊天记录里的编号，记在条目上，方便从看板跳回原话。"""
    effects: List[str] = []
    if not isinstance(status, dict):
        return effects
    status = _single_lines(status)
    who = name_of(member)

    summary = str(status.get("summary") or "").strip()
    if summary:
        log(board, member, summary)

    # 1. 表态
    for vid, vote, reason in _iter_votes(status.get("votes")):
        kind, item = find_item(board, vid)
        if not item or not is_open(kind, item):
            continue
        if vote == "disagree" and not reason:
            reason = "（未写理由）"
        previous = item["votes"].get(member, {}).get("vote")
        item["votes"][member] = _vote(member, vote, reason)
        item["updated"] = now()
        label = "同意" if vote == "agree" else "反对"
        if previous != vote or vote == "disagree":
            log(board, member, "%s %s%s" % (label, item["id"], ("：" + reason) if vote == "disagree" else ""))
        effects.append("%s %s" % (label, item["id"]))
        done = check_consensus(board, kind, item, members)
        if done:
            effects.append("%s %s" % (item["id"], "生效" if kind == "decision" else "确认"))

    # 2. 撤回自己的提议（想修改提议时，先撤回再提新版本）
    raw_withdraw = status.get("withdraw")
    if isinstance(raw_withdraw, (str, dict)):
        raw_withdraw = [raw_withdraw]
    for entry in raw_withdraw if isinstance(raw_withdraw, list) else []:
        wid = entry.get("id") if isinstance(entry, dict) else entry
        kind, item = find_item(board, wid)
        if not item:
            continue
        if item["proposer"] != member:
            log(board, "system", "%s 想撤回 %s，但提议人是 %s，已忽略" % (who, item["id"], name_of(item["proposer"])))
            continue
        if not is_open(kind, item):
            log(board, "system", "%s 已经不在讨论中，%s 的撤回已忽略" % (item["id"], who))
            continue
        item["status"] = "rejected" if kind == "decision" else "cancelled"
        item["withdrawn"] = True
        item["updated"] = now()
        log(board, member, "撤回了 %s" % item["id"])
        effects.append("撤回 %s" % item["id"])

    # 3. 新决议
    for text in _iter_texts(status.get("propose_decisions")):
        same = _find_similar(board["decisions"], "text", text)
        if same:
            if same["status"] == "discussing" and same["votes"].get(member, {}).get("vote") != "agree":
                same["votes"][member] = _vote(member, "agree")
                effects.append("同意 %s" % same["id"])
                if check_consensus(board, "decision", same, members):
                    effects.append("%s 生效" % same["id"])
            continue
        did = _next_id(board, "D")
        item = {
            "id": did, "text": clip(text, 600), "status": "discussing", "proposer": member,
            "created": now(), "updated": now(), "votes": {member: _vote(member, "agree")},
            "user_notes": [], "seq": seq,
        }
        board["decisions"].append(item)
        log(board, member, "提议 %s：%s" % (did, clip(text, 80)))
        effects.append("提议 %s" % did)
        if check_consensus(board, "decision", item, members):
            effects.append("%s 生效" % did)

    # 4. 新任务
    for entry in _iter_tasks(status.get("propose_tasks")):
        title = str(entry.get("title")).strip()
        same = _find_similar(board["tasks"], "title", title)
        if same:
            if same["status"] in OPEN_TASK and same["votes"].get(member, {}).get("vote") != "agree":
                same["votes"][member] = _vote(member, "agree")
                effects.append("同意 %s" % same["id"])
                if check_consensus(board, "task", same, members):
                    effects.append("%s 确认" % same["id"])
            continue
        owner = normalize_member(entry.get("owner")) or "unassigned"
        if owner not in members and owner != "user":
            owner = "unassigned"
        tid = _next_id(board, "T")
        item = {
            "id": tid, "title": clip(title, 300), "owner": owner,
            "deliverable": clip(str(entry.get("deliverable") or "").strip(), 300),
            "status": "proposed", "proposer": member, "created": now(), "updated": now(),
            "votes": {member: _vote(member, "agree")}, "notes": [], "user_notes": [], "seq": seq,
        }
        board["tasks"].append(item)
        log(board, member, "提议任务 %s（负责人 %s）：%s" % (tid, name_of(owner), clip(title, 60)))
        effects.append("提议 %s" % tid)
        if check_consensus(board, "task", item, members):
            effects.append("%s 确认" % tid)

    # 5. 更新自己负责的任务
    raw_updates = status.get("task_updates")
    if isinstance(raw_updates, dict):
        raw_updates = [raw_updates]
    for entry in raw_updates if isinstance(raw_updates, list) else []:
        if not isinstance(entry, dict):
            continue
        kind, item = find_item(board, entry.get("id"))
        new_status = parse_task_status(entry.get("status"))
        if kind != "task" or not item or not new_status:
            continue
        if item["owner"] != member:
            log(board, "system", "%s 想更新 %s，但负责人是 %s，已忽略" % (who, item["id"], name_of(item["owner"])))
            continue
        if item["status"] not in ACTIVE_TASK:
            log(board, "system", "%s 是「%s」状态，%s 的更新已忽略" % (item["id"], TASK_STATUS[item["status"]], who))
            continue
        note = clip(str(entry.get("note") or "").strip(), 300)
        if item["status"] == new_status and item["notes"] and item["notes"][-1].get("who") == member and item["notes"][-1].get("text") == (note or TASK_STATUS[new_status]):
            continue
        item["status"] = new_status
        item["updated"] = now()
        item["notes"].append({"who": member, "text": note or TASK_STATUS[new_status], "ts": now(), "seq": seq})
        log(board, member, "%s → %s%s" % (item["id"], TASK_STATUS[new_status], ("：" + note) if note else ""))
        effects.append("%s %s" % (item["id"], TASK_STATUS[new_status]))

    # 6. 需要群主处理的事
    for text in _iter_texts(status.get("need_user")):
        if text in ("无", "没有", "none", "None", "-"):
            continue
        if any(not n.get("resolved") and n.get("member") == member and n.get("text") == clip(text, 300) for n in board["need_user"]):
            continue
        nid = _next_id(board, "N")
        board["need_user"].append({"id": nid, "member": member, "text": clip(text, 300), "ts": now(),
                                   "resolved": False, "seq": seq})
        log(board, member, "请群主处理：%s" % clip(text, 80))
        effects.append("请群主处理")

    return effects


# ---------------------------------------------------------------- 群主的操作

def user_action(board: dict, action: str, payload: dict, members: List[str]) -> Tuple[str, Optional[str]]:
    """执行群主在看板上的操作。返回（结果说明，需要发到群里的话或 None）。出错抛 ValueError。"""
    text = str(payload.get("text") or "").strip()
    limit = {"set_goal": 2000, "add_decision": 600, "edit": 600,
             "comment": 1000, "resolve_need": 1000}.get(action)
    if limit and len(text) > limit:
        raise ValueError("内容最多 %d 字" % limit)
    if action == "add_task" and (len(str(payload.get("title") or text).strip()) > 300
                                or len(str(payload.get("deliverable") or "").strip()) > 300):
        raise ValueError("任务和交付物最多各 300 字")

    if action == "set_goal":
        board["goal"] = clip(text, 2000)
        log(board, "user", "修改了群目标")
        return "群目标已更新", ("（修改了群目标）" + text) if text else None

    if action == "add_decision":
        if not text:
            raise ValueError("请填写决议内容")
        did = _next_id(board, "D")
        board["decisions"].append({
            "id": did, "text": clip(text, 600), "status": "effective", "proposer": "user",
            "created": now(), "updated": now(), "votes": {}, "user_notes": [],
        })
        log(board, "user", "直接确定 %s：%s" % (did, clip(text, 80)))
        return "已添加 %s" % did, "（群主直接确定 %s）%s" % (did, text)

    if action == "add_task":
        title = str(payload.get("title") or text).strip()
        if not title:
            raise ValueError("请填写任务内容")
        owner = normalize_member(payload.get("owner")) or "unassigned"
        tid = _next_id(board, "T")
        board["tasks"].append({
            "id": tid, "title": clip(title, 300), "owner": owner,
            "deliverable": clip(str(payload.get("deliverable") or "").strip(), 300),
            "status": "todo", "proposer": "user", "created": now(), "updated": now(),
            "votes": {}, "notes": [], "user_notes": [],
        })
        log(board, "user", "直接分配任务 %s 给 %s：%s" % (tid, name_of(owner), clip(title, 60)))
        return "已添加 %s" % tid, "（群主分配任务 %s 给 %s）%s" % (tid, name_of(owner), title)

    if action == "resolve_need":
        nid = normalize_id(payload.get("id")) or ""
        for need in board["need_user"]:
            if need["id"] == nid:
                need["resolved"] = True
                need["reply"] = clip(text, 1000)
                log(board, "user", "处理了 %s 的请求%s" % (name_of(need["member"]), ("：" + clip(text, 60)) if text else ""))
                note = None
                if text:
                    note = "（回复 %s：%s）%s" % (name_of(need["member"]), clip(need["text"], 40), text)
                return "已处理", note
        raise ValueError("找不到这条请求")

    kind, item = find_item(board, payload.get("id"))
    if not item:
        raise ValueError("找不到这个条目")
    iid = item["id"]

    if action == "comment":
        if not text:
            raise ValueError("请写下你的意见")
        item.setdefault("user_notes", []).append({"text": clip(text, 1000), "ts": now(), "pending": True})
        if kind == "decision":
            item["status"] = "discussing"
        elif item["status"] != "cancelled":
            item["status"] = "proposed"
        item["votes"] = {}
        item["updated"] = now()
        log(board, "user", "对 %s 提了意见，已退回讨论：%s" % (iid, clip(text, 60)))
        return "%s 已退回讨论，三家需要先回应你的意见" % iid, "（对 %s 的意见）%s" % (iid, text)

    if action == "force":
        item["status"] = "effective" if kind == "decision" else "todo"
        for note in item.get("user_notes", []):
            note["pending"] = False
        item["updated"] = now()
        log(board, "user", "直接确认了 %s" % iid)
        return "%s 已确认" % iid, "（群主直接确认了 %s）" % iid

    if action == "reject":
        item["status"] = "rejected" if kind == "decision" else "cancelled"
        item["updated"] = now()
        log(board, "user", "否决了 %s%s" % (iid, ("：" + clip(text, 60)) if text else ""))
        return "%s 已否决" % iid, "（群主否决了 %s）%s" % (iid, text)

    if action == "reopen":
        item["status"] = "discussing" if kind == "decision" else "proposed"
        item["votes"] = {}
        item["updated"] = now()
        log(board, "user", "重新打开了 %s" % iid)
        return "%s 已重新打开" % iid, "（群主重新打开了 %s，请大家重新表态）%s" % (iid, text)

    if action == "edit":
        if not text:
            raise ValueError("内容不能为空")
        field = "text" if kind == "decision" else "title"
        item[field] = clip(text, 600)
        item["updated"] = now()
        log(board, "user", "修改了 %s 的内容" % iid)
        return "%s 已修改" % iid, None

    if action == "delete":
        board["decisions" if kind == "decision" else "tasks"].remove(item)
        log(board, "user", "删除了 %s" % iid)
        return "%s 已删除" % iid, None

    if action == "set_owner" and kind == "task":
        owner = normalize_member(payload.get("owner")) or "unassigned"
        item["owner"] = owner
        item["updated"] = now()
        log(board, "user", "把 %s 的负责人改为 %s" % (iid, name_of(owner)))
        return "已修改负责人", "（群主把 %s 的负责人改为 %s）" % (iid, name_of(owner))

    if action == "set_status" and kind == "task":
        status = payload.get("status")
        if status not in TASK_STATUS:
            raise ValueError("状态不合法")
        item["status"] = status
        item["updated"] = now()
        log(board, "user", "把 %s 改为%s" % (iid, TASK_STATUS[status]))
        return "已修改状态", None

    raise ValueError("不支持的操作")


# ---------------------------------------------------------------- 交给成员看的看板文本

def _votes_line(item: dict, members: List[str]) -> str:
    parts = []
    for m in members:
        v = item["votes"].get(m)
        if not v:
            parts.append("%s 未表态" % name_of(m))
        elif v["vote"] == "agree":
            parts.append("%s 同意" % name_of(m))
        else:
            parts.append("%s 反对（%s）" % (name_of(m), v.get("reason") or "未写理由"))
    return " · ".join(parts)


def render_markdown(board: dict, members: List[str]) -> str:
    board = _single_lines(board)  # Also isolate text saved by earlier versions.
    lines: List[str] = []
    lines.append("### 群目标")
    lines.append(board.get("goal") or "（群主还没写）")

    needs = [n for n in board["need_user"] if not n.get("resolved")]
    if needs:
        lines.append("")
        lines.append("### 等群主处理")
        for n in needs[-10:]:
            lines.append("- %s 提出：%s" % (name_of(n["member"]), n["text"]))

    lines.append("")
    lines.append("### 决议")
    open_d = [d for d in board["decisions"] if d["status"] == "discussing"]
    done_d = [d for d in board["decisions"] if d["status"] == "effective"]
    rejected = [d for d in board["decisions"] if d["status"] == "rejected"]
    if not (open_d or done_d or rejected):
        lines.append("（还没有）")
    for d in open_d:
        lines.append("- %s【讨论中】%s" % (d["id"], d["text"]))
        lines.append("  表态：%s" % _votes_line(d, members))
        for note in d.get("user_notes", []):
            if note.get("pending"):
                lines.append("  【群主意见·工具标注】：%s" % note["text"])
    for d in done_d[-30:]:
        lines.append("- %s【已生效】%s" % (d["id"], d["text"]))
    for d in rejected[-5:]:
        lines.append("- %s【%s】%s" % (d["id"], "已撤回" if d.get("withdrawn") else "已否决", clip(d["text"], 60)))

    lines.append("")
    lines.append("### 任务")
    proposed = [t for t in board["tasks"] if t["status"] in OPEN_TASK]
    active = [t for t in board["tasks"] if t["status"] in ACTIVE_TASK]
    finished = [t for t in board["tasks"] if t["status"] == "done"]
    if not (proposed or active or finished):
        lines.append("（还没有）")
    for t in proposed:
        lines.append("- %s【提议中】负责人：%s｜%s%s" % (
            t["id"], name_of(t["owner"]), t["title"], ("｜交付：" + t["deliverable"]) if t.get("deliverable") else ""))
        lines.append("  表态：%s" % _votes_line(t, members))
        for note in t.get("user_notes", []):
            if note.get("pending"):
                lines.append("  【群主意见·工具标注】：%s" % note["text"])
    for t in active:
        last = t["notes"][-1]["text"] if t.get("notes") else ""
        lines.append("- %s【%s】负责人：%s｜%s%s%s" % (
            t["id"], TASK_STATUS[t["status"]], name_of(t["owner"]), t["title"],
            ("｜交付：" + t["deliverable"]) if t.get("deliverable") else "",
            ("｜最新进展：" + last) if last else ""))
    for t in finished[-10:]:
        last = t["notes"][-1]["text"] if t.get("notes") else ""
        lines.append("- %s【已完成】负责人：%s｜%s%s" % (
            t["id"], name_of(t["owner"]), clip(t["title"], 80), ("｜" + clip(last, 80)) if last else ""))

    recent = board["changelog"][-10:]
    if recent:
        lines.append("")
        lines.append("### 最近变更")
        for entry in recent:
            lines.append("- %s %s：%s" % (fmt_time(entry["ts"]), name_of(entry["who"]), entry["text"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- 从回复里取出状态更新

_STATUS_BLOCK = re.compile(r"<<<\s*STATUS\b(?P<body>.*?)STATUS\s*>>>", re.S | re.I)
_STATUS_OPEN = re.compile(r"<<<\s*STATUS\b(?P<body>.*)$", re.S | re.I)
_TRAILING_LABEL = re.compile(
    r"(\n\s*(\*\*|__|#+)?\s*[【\[]?\s*(状态更新|STATUS)\s*[】\]]?\s*[:：]?\s*(\*\*|__)?\s*[:：]?\s*)+$", re.I)
_EMPTY_FENCE = re.compile(r"```[a-zA-Z]*\s*```\s*$")


def _decode_status(candidate: str) -> Any:
    try:
        value = json.loads(candidate)
    except RecursionError as exc:
        raise ValueError("状态更新嵌套过深") from exc
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 64:
            raise ValueError("状态更新嵌套超过 64 层")
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
    return value


def _loads_lenient(body: str) -> Any:
    if len(body.encode("utf-8")) > 65536:
        raise ValueError("状态更新超过 64 KB")
    body = body.strip()
    body = re.sub(r"^```[a-zA-Z]*\s*", "", body)
    body = re.sub(r"\s*```$", "", body)
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("没有找到 JSON")
    candidate = body[start:end + 1]
    try:
        return _decode_status(candidate)
    except json.JSONDecodeError:
        fixed = (candidate.replace("“", '"').replace("”", '"')
                 .replace("‘", "'").replace("’", "'"))
        fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
        return _decode_status(fixed)


def extract_status(text: str) -> Tuple[str, Optional[dict], Optional[str]]:
    """返回（正文、状态更新、错误说明）。没有状态更新时第二项为 None。"""
    text = text or ""
    matches = list(_STATUS_BLOCK.finditer(text))
    if matches:
        body = matches[-1].group("body")
        visible = _STATUS_BLOCK.sub("", text)
    else:
        m = _STATUS_OPEN.search(text)
        if not m:
            return text.strip(), None, None
        body = m.group("body")
        visible = text[:m.start()]
    visible = visible.rstrip()
    for _ in range(3):
        visible = _EMPTY_FENCE.sub("", visible).rstrip()
        visible = re.sub(r"\n\s*(```[a-zA-Z]*|---+)\s*$", "", visible).rstrip()
        visible = _TRAILING_LABEL.sub("", visible).rstrip()
    try:
        data = _loads_lenient(body)
    except (ValueError, RecursionError) as exc:
        return visible.strip(), None, "状态更新格式有误：%s" % exc
    if not isinstance(data, dict):
        return visible.strip(), None, "状态更新不是对象"
    return visible.strip(), data, None
