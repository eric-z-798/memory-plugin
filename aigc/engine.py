"""群聊引擎：安排谁发言、什么时候发言，并维护共享看板。

每个群一个 GroupRunner，里面是一条任务队列和一个后台线程：
- 群主发消息 → 排一轮发言（被 @ 的成员，或全体）；
- 依次模式：一个接一个，后面的人能看到前面的发言；
- 背靠背模式：同一份材料同时作答，互相看不到，答完再互评；
- 一轮结束后：需要时请书记员压缩前情摘要；还有人没表态或有反对意见时，自动续轮（有上限）。
"""

from __future__ import annotations

import re
import copy
import threading
import traceback
from collections import deque
from typing import List, Optional

from . import board as board_mod
from . import prompts
from .common import MEMBER_ORDER, clip, name_of, normalize_member, now
from .members import CancelToken, Cancelled, MemberError
from .store import StoreError
from .memory import MemoryError

MENTION_RE = re.compile(r"[@＠]\s?(claude|chatgpt|codex|gpt|gemini|全体成员|全体|所有人|大家|all|everyone)", re.I)
ALL_WORDS = {"全体成员", "全体", "所有人", "大家", "all", "everyone"}

GROUP_FIELDS = {
    "name": str, "members": list, "mode": str, "parallel_review": bool, "secretary": str,
    "reply_length": str, "auto_rounds": int, "language": str,
}


def parse_mentions(text: str) -> Optional[List[str]]:
    """返回被 @ 的成员列表；没有 @ 或 @全体 时返回 None（表示全体）。"""
    found: List[str] = []
    for m in MENTION_RE.finditer(text or ""):
        key = m.group(1).lower()
        if key in ALL_WORDS:
            return None
        mid = normalize_member(key)
        if mid and mid in MEMBER_ORDER and mid not in found:
            found.append(mid)
    return found or None


class GroupRunner:
    def __init__(self, engine: "Engine", gid: str):
        self.engine = engine
        self.store = engine.store
        self.gid = gid
        self._lock = threading.Lock()
        self._jobs: deque = deque()
        self._worker = False
        self._cancel: Optional[CancelToken] = None
        self._speaking: dict = {}
        self._phase = ""
        self._since = 0.0
        # 每位成员最近一次成功发言时看到的最后一条消息编号，用来避免重复发言
        self.seen: dict = {}
        self._memory_context: dict = {}

    # ----- 对外
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self._worker,
                "phase": self._phase,
                "speaking": [{"member": m, "since": t} for m, t in self._speaking.items()],
                "queued": len(self._jobs),
                "since": self._since,
            }

    def submit(self, job: dict) -> None:
        job.setdefault("auto", 0)
        with self._lock:
            if not job["auto"]:
                # 群主的新指令优先，排队中的自动续轮作废
                self._jobs = deque(j for j in self._jobs if not j.get("auto"))
                last = self._jobs[-1] if self._jobs else None
                if last is not None:
                    self._merge(last, job)
                else:
                    self._jobs.append(job)
            else:
                self._jobs.append(job)
            if not self._worker:
                self._worker = True
                threading.Thread(target=self._work, name="aigc-" + self.gid, daemon=True).start()

    @staticmethod
    def _merge(last: dict, job: dict) -> None:
        """排队中的一轮还没开始，新消息并进去即可：那一轮的发言人会看到所有新消息。"""
        if last["participants"] is None or job["participants"] is None:
            last["participants"] = None
        else:
            for m in job["participants"]:
                if m not in last["participants"]:
                    last["participants"].append(m)
        last["mentioned"] = list(dict.fromkeys(last.get("mentioned", []) + job.get("mentioned", [])))
        continuing = "continue" in (last["task"], job["task"])
        if last["task"] != job["task"]:
            last["task"] = "reply"
        triggers = [t for t in (last.get("trigger"), job.get("trigger")) if t]
        # 点了「继续讨论」就让大家都发言，不按"是否看过新消息"筛选
        last["trigger"] = max(triggers) if triggers and not continuing else None

    def stop(self) -> bool:
        with self._lock:
            busy = self._worker or bool(self._jobs)
            self._jobs.clear()
            if self._cancel:
                self._cancel.cancel()
            return busy

    # ----- 后台线程
    def _work(self) -> None:
        while True:
            with self._lock:
                if not self._jobs:
                    self._worker = False
                    self._cancel = None
                    self._phase = ""
                    self._speaking = {}
                    return
                job = self._jobs.popleft()
                cancel = self._cancel = CancelToken()
                self._since = now()
            try:
                if self.store.exists(self.gid):
                    self._run_round(job, cancel)
                    if not cancel.cancelled:
                        self._maybe_summarize(cancel)
                    if not cancel.cancelled:
                        self._maybe_auto_continue(job)
            except StoreError:
                pass  # 群在发言过程中被删除了
            except Exception as exc:  # 兜底：任何意外都不能让群卡死
                traceback.print_exc()
                try:
                    self._system("出错了：%s" % exc)
                except Exception:
                    pass
            finally:
                with self._lock:
                    self._speaking = {}
                    self._phase = ""

    def _set_phase(self, phase: str) -> None:
        with self._lock:
            self._phase = phase

    def _system(self, text: str, subtle: bool = False) -> None:
        self.store.append_message(self.gid, {"kind": "system", "author": "system", "text": text, "subtle": subtle})

    # ----- 一轮发言
    def _run_round(self, job: dict, cancel: CancelToken) -> None:
        with self.store.lock(self.gid):
            group = self.store.load_group(self.gid)
            members = [m for m in group.get("members", []) if m in MEMBER_ORDER]
            if not members:
                self._system("群里还没有 AI 成员，请在顶部点亮至少一位。")
                return
            if job["participants"] is None:
                k = group.get("round_counter", 0) % len(members)
                participants = members[k:] + members[:k]  # 轮流先发言，避免总是同一家定调
            else:
                participants = [m for m in job["participants"] if m in members]
                missing = [m for m in job["participants"] if m not in members]
                if missing:
                    self._system("%s 不在群里，可以在顶部点亮后再 @。" % "、".join(name_of(m) for m in missing))
            trigger = job.get("trigger")
            if trigger:
                with self._lock:
                    participants = [m for m in participants if self.seen.get(m, 0) < trigger]
            if not participants:
                return
            group["round_counter"] = group.get("round_counter", 0) + 1
            self.store.save_group(group)
            mode = group.get("mode", "sequential")
            review = group.get("parallel_review", True)
            messages = self.store.messages(self.gid)
            query = next((m.get("text", "") for m in reversed(messages) if m.get("kind") == "user"), "")
            # All members in this round receive the same durable memory version.
            self._memory_context = self.store.memory.context(group["project_id"], query, budget=6000)

        if mode == "parallel" and len(participants) > 1:
            self._parallel_round(participants, job, cancel, review)
        else:
            self._set_phase("依次发言")
            for member in participants:
                if cancel.cancelled:
                    return
                self._speak(member, self._task_for(member, job), cancel)

    @staticmethod
    def _task_for(member: str, job: dict) -> str:
        if member in job.get("mentioned", []):
            return "mention"
        return job.get("task") or "reply"

    def _parallel_round(self, participants: List[str], job: dict, cancel: CancelToken, review: bool) -> None:
        msgs = self.store.messages(self.gid)
        snapshot = msgs[-1]["seq"] if msgs else 0
        task = job.get("task") or "reply"
        self._set_phase("背靠背作答")
        ok = self._speak_together(participants, lambda m: "parallel" if task in ("reply", "mention") else task,
                                  cancel, snapshot)
        # 补表态的自动续轮不需要互评
        if cancel.cancelled or not review or task == "vote" or len(ok) < 2:
            return
        msgs = self.store.messages(self.gid)
        snapshot = msgs[-1]["seq"] if msgs else 0
        self._set_phase("互评")
        self._speak_together(ok, lambda m: "review", cancel, snapshot)

    def _speak_together(self, members: List[str], task_of, cancel: CancelToken, upto: int) -> List[str]:
        ok: List[str] = []
        lock = threading.Lock()
        # Freeze the board too: a fast answer must not leak into a slower peer's prompt.
        with self.store.lock(self.gid):
            frozen = copy.deepcopy({"group": self.store.load_group(self.gid),
                                    "settings": self.store.load_settings(),
                                    "board": self.store.load_board(self.gid),
                                    "messages": self.store.messages(self.gid)})

        def work(member):
            try:
                if self._speak(member, task_of(member), cancel, upto_seq=upto, frozen=frozen):
                    with lock:
                        ok.append(member)
            except StoreError:
                pass
            except Exception as exc:
                traceback.print_exc()
                if not cancel.cancelled and self.store.exists(self.gid):
                    self._system("%s 这次未完成：%s" % (name_of(member), exc))

        threads = [threading.Thread(target=work, args=(m,), daemon=True) for m in members]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return [m for m in members if m in ok]

    def _speak(self, member: str, task: str, cancel: CancelToken, upto_seq: Optional[int] = None,
               frozen: Optional[dict] = None) -> bool:
        start = now()
        with self._lock:
            self._speaking[member] = start
        try:
            group = frozen["group"] if frozen else self.store.load_group(self.gid)
            settings = frozen["settings"] if frozen else self.store.load_settings()
            board = frozen["board"] if frozen else self.store.load_board(self.gid)
            msgs = frozen["messages"] if frozen else self.store.messages(self.gid)
            seen_seq = upto_seq if upto_seq is not None else (msgs[-1]["seq"] if msgs else 0)
            prompt = prompts.build_member_prompt(group, board, msgs, member, task, settings, upto_seq,
                                                  memory_context=self._memory_context)
            try:
                raw = self.engine.registry.adapter(member, settings).ask(
                    prompt, self.store.workspace(self.gid), cancel)
            except Cancelled:
                return False
            except MemberError as exc:
                if not cancel.cancelled:
                    self._system("%s 这次没能回复：%s" % (name_of(member), exc))
                return False
            if cancel.cancelled:
                return False

            visible, status, err = board_mod.extract_status(raw)
            with self.store.lock(self.gid):
                if cancel.cancelled:
                    return False
                board = self.store.load_board(self.gid)
                members = self.store.load_group(self.gid).get("members", [])
                current = self.store.messages(self.gid)
                next_seq = (current[-1]["seq"] + 1) if current else 1  # 持有锁，这就是这条发言的编号
                effects = board_mod.apply_status(board, member, status, members, next_seq) if status else []
                note = err or ("" if status else "没有附状态更新，看板未变化")
                if note:
                    board_mod.log(board, "system", "%s：%s" % (name_of(member), note))
                self.store.save_board(self.gid, board)
                proposed = (status or {}).get("memories", [])
                candidates = [{"content": entry["content"], "kind": entry.get("kind", "note")}
                              for entry in proposed[:5]
                              if isinstance(entry, dict) and isinstance(entry.get("content"), str)] if isinstance(proposed, list) else []
                message = self.store.append_message(self.gid, {
                    "kind": "chat", "author": member, "task": task,
                    "text": visible or "（只更新了看板，没有正文）",
                    "effects": effects, "status_note": note, "elapsed": round(now() - start, 1),
                    "memory_revision": self._memory_context.get("revision"),
                    "memory_ids": [m["id"] for m in self._memory_context.get("items", [])],
                    "memory_proposals": candidates,
                })
                # AI proposals stay pending, even when other AI members repeat or agree.
                if isinstance(candidates, list):
                    for index, candidate in enumerate(candidates[:5]):
                        if not isinstance(candidate, dict) or not isinstance(candidate.get("content"), str):
                            continue
                        try:
                            self.store.memory.add(group["project_id"], candidate["content"],
                                kind=candidate.get("kind", "note"), status="pending",
                                source={"group_id": self.gid, "seq": message["seq"], "author": member},
                                idempotency_key="%s:%s:memory:%s" % (self.gid, message["id"], index))
                        except MemoryError as exc:
                            self._system("%s 的一条候选记忆未保存：%s" % (name_of(member), exc), subtle=True)
            with self._lock:
                self.seen[member] = max(self.seen.get(member, 0), seen_seq)
            return True
        finally:
            with self._lock:
                self._speaking.pop(member, None)

    # ----- 前情摘要
    def _maybe_summarize(self, cancel: CancelToken) -> None:
        settings = self.store.load_settings()
        window, batch = settings.get("recent_window", 16), settings.get("summary_batch", 8)
        board = self.store.load_board(self.gid)
        pending = [m for m in self.store.messages(self.gid)
                   if m.get("kind") in ("chat", "user") and m["seq"] > board.get("summary_upto", 0)]
        if len(pending) - window < batch:
            return
        fold = pending[:len(pending) - window]
        group = self.store.load_group(self.gid)
        members = [m for m in group.get("members", []) if m in MEMBER_ORDER]
        if not members:
            return
        fixed = group.get("secretary")
        if fixed in members:
            order = [fixed] + [m for m in members if m != fixed]
        else:
            k = group.get("secretary_index", 0) % len(members)
            order = members[k:] + members[:k]

        self._set_phase("整理前情摘要")
        for sec in order:
            if cancel.cancelled:
                return
            with self._lock:
                self._speaking[sec] = now()
            try:
                prompt = prompts.build_summary_prompt(group, board, fold, sec)
                raw = self.engine.registry.adapter(sec, settings).ask(prompt, self.store.workspace(self.gid), cancel)
            except Cancelled:
                return
            except MemberError:
                continue
            finally:
                with self._lock:
                    self._speaking.pop(sec, None)
            text = board_mod.extract_status(raw)[0].strip()
            if not text or cancel.cancelled:
                continue
            upto = fold[-1]["seq"]
            with self.store.lock(self.gid):
                board = self.store.load_board(self.gid)
                board["summary"] = clip(text, 3000)
                board["summary_upto"] = upto
                board["summary_by"] = sec
                board["summary_ts"] = now()
                board_mod.log(board, sec, "更新了前情摘要（截至 #%d）" % upto)
                self.store.save_board(self.gid, board)
                group = self.store.load_group(self.gid)
                group["secretary_index"] = group.get("secretary_index", 0) + 1
                self.store.save_group(group)
            self._system("%s 整理了前情摘要（截至 #%d），可以在看板里查看。" % (name_of(sec), upto), subtle=True)
            return
        self._system("这次前情摘要没有整理成功，下一轮会再试。", subtle=True)

    # ----- 自动续轮
    def _maybe_auto_continue(self, job: dict) -> None:
        group = self.store.load_group(self.gid)
        limit = int(group.get("auto_rounds", 2))
        used = job.get("auto", 0)
        if used >= limit:
            return
        with self._lock:
            if self._jobs:  # 群主已经有新的指令在排队
                return
        members = [m for m in group.get("members", []) if m in MEMBER_ORDER]
        board = self.store.load_board(self.gid)
        owing, disputed = [], False
        items = [("decision", d) for d in board["decisions"]] + [("task", t) for t in board["tasks"]]
        for kind, item in items:
            if not board_mod.is_open(kind, item):
                continue
            for m in members:
                vote = item["votes"].get(m, {}).get("vote")
                if vote is None and m not in owing:
                    owing.append(m)
                elif vote == "disagree":
                    disputed = True
        if owing:
            owing = [m for m in members if m in owing]
            self._system("自动续轮（%d/%d）：请 %s 对未表态的条目表态。" % (used + 1, limit, "、".join(name_of(m) for m in owing)), subtle=True)
            self.submit({"type": "round", "participants": owing, "task": "vote", "auto": used + 1, "trigger": None})
        elif disputed:
            self._system("自动续轮（%d/%d）：有条目存在反对意见，请大家针对分歧继续讨论。" % (used + 1, limit), subtle=True)
            self.submit({"type": "round", "participants": None, "task": "debate", "auto": used + 1, "trigger": None})


class Engine:
    def __init__(self, store, registry):
        self.store = store
        self.registry = registry
        self._runners: dict = {}
        self._lock = threading.Lock()

    def runner(self, gid: str) -> GroupRunner:
        with self._lock:
            if gid not in self._runners:
                self._runners[gid] = GroupRunner(self, gid)
            return self._runners[gid]

    def state(self, gid: str) -> dict:
        return self.runner(gid).snapshot()

    def post_user_message(self, gid: str, text: str) -> dict:
        msg = self.store.append_message(gid, {"kind": "user", "author": "user", "text": text})
        mentioned = parse_mentions(text)
        self.runner(gid).submit({
            "type": "round", "participants": list(mentioned) if mentioned else None,
            "mentioned": list(mentioned or []), "task": "mention" if mentioned else "reply",
            "trigger": msg["seq"],
        })
        return msg

    def continue_discussion(self, gid: str) -> None:
        self.runner(gid).submit({"type": "round", "participants": None, "task": "continue", "trigger": None})

    def stop(self, gid: str) -> bool:
        busy = self.runner(gid).stop()
        if busy:
            self.store.append_message(gid, {"kind": "system", "author": "system", "text": "群主停止了本轮发言。"})
        return busy

    def board_action(self, gid: str, action: str, payload: dict, respond: bool = False) -> str:
        with self.store.lock(gid):
            board = self.store.load_board(gid)
            group = self.store.load_group(gid)
            result, note = board_mod.user_action(board, action, payload, group.get("members", []))
            self.store.save_board(gid, board)
            msg = None
            if note:
                msg = self.store.append_message(gid, {"kind": "user", "author": "user", "text": note, "board_note": True})
        if respond and msg:
            self.runner(gid).submit({"type": "round", "participants": None, "task": "reply", "trigger": msg["seq"]})
        return result

    def update_group(self, gid: str, changes: dict) -> dict:
        with self.store.lock(gid):
            group = self.store.load_group(gid)
            if "project_id" in changes and changes["project_id"] != group["project_id"]:
                if self.state(gid)["running"] or any(m.get("kind") in ("user", "chat") for m in self.store.messages(gid)):
                    raise ValueError("已有聊天记录或正在发言，请新建群聊并选择该项目")
                project_id = changes["project_id"]
                if not isinstance(project_id, str) or project_id not in {p["id"] for p in self.store.memory.projects()}:
                    raise ValueError("找不到所选项目")
                group["project_id"] = project_id
            before = list(group.get("members", []))
            for key, typ in GROUP_FIELDS.items():
                if key not in changes:
                    continue
                value = changes[key]
                if key == "name":
                    value = str(value or "").strip()[:60] or group["name"]
                elif key == "members":
                    picked = [normalize_member(v) for v in value] if isinstance(value, list) else []
                    value = [m for m in MEMBER_ORDER if m in picked]
                elif key == "mode":
                    value = value if value in ("sequential", "parallel") else group.get("mode", "sequential")
                elif key == "secretary":
                    value = value if value in MEMBER_ORDER + ["rotate"] else "rotate"
                elif key == "reply_length":
                    value = value if value in prompts.REPLY_LENGTHS else "medium"
                elif key == "auto_rounds":
                    try:
                        value = max(0, min(5, int(value)))
                    except (TypeError, ValueError):
                        continue
                elif key == "language":
                    value = str(value or "").strip()[:20] or "简体中文"
                elif typ is bool:
                    value = bool(value)
                group[key] = value
            self.store.save_group(group)
            if group.get("members", []) != before:
                board = self.store.load_board(gid)
                board_mod.log(board, "user", "调整了成员：%s" % ("、".join(name_of(m) for m in group["members"]) or "（无）"))
                for text in board_mod.recheck_all(board, group["members"]):
                    self.store.append_message(gid, {"kind": "system", "author": "system", "text": text, "subtle": True})
                self.store.save_board(gid, board)
                self.store.append_message(gid, {
                    "kind": "system", "author": "system", "subtle": True,
                    "text": "在群成员：%s" % ("、".join(name_of(m) for m in group["members"]) or "（无）"),
                })
        return group

    def shutdown(self) -> None:
        with self._lock:
            runners = list(self._runners.values())
        for r in runners:
            r.stop()
