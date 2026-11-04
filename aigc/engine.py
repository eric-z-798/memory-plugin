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
import sys
import threading
import traceback
from collections import deque
from typing import List, Optional

from . import board as board_mod
from . import prompts
from .common import MEMBER_ORDER, clip, name_of, normalize_member, now
from .members import CancelToken, Cancelled, MemberError, classify_cli_failure
from .store import StoreError
from .memory import MemoryError
from .routing import clean_overrides, resolve_route, route_settings
from .availability import AvailabilityStore
from .quota import QuotaMonitor

MENTION_RE = re.compile(r"(?<![A-Za-z0-9_.+\-@＠])[@＠]\s?(claude|chatgpt|codex|gpt|gemini|全体成员|全体|所有人|大家|all|everyone)(?![A-Za-z0-9_-])(?!\.[A-Za-z0-9])", re.I)
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
    def __init__(self, engine: "Engine", gid: str, progress: dict):
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
        self.seen: dict = {m: entry["seen"] for m, entry in progress.items() if "seen" in entry}
        self._memory_context: dict = {}
        self._routing_snapshot = None
        self._paused_at_round_start = set()
        self._failed_members = set()
        self._trigger_seq = None

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

    def submit(self, job: dict, require_idle: bool = False) -> None:
        job.setdefault("auto", 0)
        job.setdefault("context_trigger", job.get("trigger"))
        with self._lock:
            if require_idle and (self._worker or self._jobs):
                raise ValueError("请等当前讨论结束后再补答")
            if not job["auto"]:
                # 群主的新指令优先，排队中的自动续轮作废
                self._jobs = deque(j for j in self._jobs if not j.get("auto"))
                last = self._jobs[-1] if self._jobs else None
                if last is not None and not (last.get("retry_only") or job.get("retry_only")):
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
        context = [t for t in (last.get("context_trigger"), job.get("context_trigger")) if t]
        last["context_trigger"] = min(context) if context else None
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
                    if not cancel.cancelled and not job.get("retry_only"):
                        self._maybe_summarize(cancel)
                    if not cancel.cancelled and not job.get("retry_only"):
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

    def _system(self, text: str, subtle: bool = False, model_route: Optional[dict] = None,
                retry_member: Optional[str] = None, task: str = "reply") -> None:
        message = {"kind": "system", "author": "system", "text": text, "subtle": subtle}
        if model_route is not None:
            message["model_route"] = model_route
        if retry_member is not None:
            message.update(incomplete=True, retry_member=retry_member, task=task)
        self.store.append_message(self.gid, message)

    def _saved_warning(self, text: str) -> None:
        try:
            self._system(text, subtle=True)
        except Exception:
            # A second storage failure must not turn durable output into a retry.
            # The console retains the warning when disk storage is unavailable.
            print(text, file=sys.stderr)
            traceback.print_exc()

    def _select_model(self, member, group, settings, task, query, prompt):
        # Freeze host choices for the whole round, including peer review and summary.
        if self._routing_snapshot is not None:
            group = self._routing_snapshot["group"]
            settings = self._routing_snapshot["settings"]
            query = self._routing_snapshot["query"]
        route = resolve_route(member, settings, group, task, query, context_chars=len(prompt))
        return route, route_settings(settings, member, route)

    # ----- 一轮发言
    def _run_round(self, job: dict, cancel: CancelToken) -> None:
        self._routing_snapshot = None
        if not job.get("auto"):
            self._failed_members.clear()
            self._trigger_seq = job.get("context_trigger", job.get("trigger"))
        self._paused_at_round_start = {m for m, state in self.engine.availability.snapshot().items() if state["paused"]}
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
            if job.get("auto"):
                participants = [m for m in participants if m not in self._failed_members]
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
            self._routing_snapshot = copy.deepcopy({"group": group, "settings": self.store.load_settings(), "query": query})

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
                    self._failed_members.add(member)
                    self._system("%s 这次未完成：%s" % (name_of(member), exc), retry_member=member, task=task_of(member))

        threads = [threading.Thread(target=work, args=(m,), daemon=True) for m in members]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return [m for m in members if m in ok]

    def _speak(self, member: str, task: str, cancel: CancelToken, upto_seq: Optional[int] = None,
               frozen: Optional[dict] = None) -> bool:
        if member in self._paused_at_round_start or self.engine.availability.snapshot()[member]["paused"]:
            self._system("%s 已暂停，本轮跳过；其评审仍待补充。可在模型设置中恢复。" % name_of(member), subtle=True)
            return False
        start = now()
        message = None
        with self._lock:
            self._speaking[member] = start
        try:
            group = frozen["group"] if frozen else self.store.load_group(self.gid)
            settings = frozen["settings"] if frozen else self.store.load_settings()
            board = frozen["board"] if frozen else self.store.load_board(self.gid)
            msgs = frozen["messages"] if frozen else self.store.messages(self.gid)
            seen_seq = upto_seq if upto_seq is not None else (msgs[-1]["seq"] if msgs else 0)
            trigger = self._trigger_seq or next((m["seq"] for m in reversed(msgs) if m.get("author") == "user"), 0)
            progress = self.store.begin_member_context(self.gid, member, trigger)
            context_state = {}
            prompt = prompts.build_member_prompt(group, board, msgs, member, task, settings, upto_seq,
                memory_context=self._memory_context, trigger_seq=self._trigger_seq,
                seen_seq=progress.get("seen"), unread_from=progress.get("unread_from"), context_state=context_state)
            route = None
            try:
                query = next((m.get("text", "") for m in reversed(msgs) if m.get("kind") == "user"), "")
                route, selected = self._select_model(member, group, settings, task, query, prompt)
                route["quota_snapshot"] = self.engine.quota.get(member, selected)
                if cancel.cancelled:
                    raise Cancelled()
                if self.engine.availability.snapshot()[member]["paused"]:
                    self._system("%s 已暂停，本次请求未发出。" % name_of(member), retry_member=member, task=task, model_route=route)
                    return False
                adapter = self.engine.registry.adapter(member, selected)
                raw = adapter.ask(prompt, self.store.workspace(self.gid), cancel)
                route.update(getattr(adapter, "last_metadata", {}))
            except Cancelled:
                self._system("%s 的本次发言已中断，尚未完成。" % name_of(member), retry_member=member, task=task, model_route=route)
                return False
            except (MemberError, ValueError) as exc:
                if not cancel.cancelled:
                    self._failed_members.add(member)
                    if isinstance(exc, MemberError):
                        self.engine.record_member_failure(member, exc)
                    paused = self.engine.availability.snapshot()[member]["paused"]
                    suffix = "；已暂停该成员的新调用，其他成员可继续。" if paused else ""
                    self._system("%s 这次没能回复：%s%s" % (name_of(member), exc, suffix), model_route=route, retry_member=member, task=task)
                else:
                    self._system("%s 的本次发言已中断，尚未完成。" % name_of(member), retry_member=member, task=task, model_route=route)
                return False
            if cancel.cancelled:
                self._system("%s 的本次发言已中断，尚未完成。" % name_of(member), retry_member=member, task=task, model_route=route)
                return False

            visible, status, err = board_mod.extract_status(raw)
            with self.store.lock(self.gid):
                if cancel.cancelled:
                    self._system("%s 的本次发言已中断，尚未完成。" % name_of(member), retry_member=member, task=task, model_route=route)
                    return False
                board = self.store.load_board(self.gid)
                members = self.store.load_group(self.gid).get("members", [])
                staged = copy.deepcopy(board)
                effects = board_mod.apply_status(staged, member, status, members) if status else []
                note = err or ("" if status else "没有附状态更新，看板未变化")
                if note:
                    board_mod.log(staged, "system", "%s：%s" % (name_of(member), note))
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
                    "model_route": route,
                })
                # The durable message supplies the identity for every new board reference.
                def bind_sources(value):
                    if isinstance(value, dict):
                        if "seq" in value and value["seq"] is None:
                            value["seq"] = message["seq"]
                        for child in value.values():
                            bind_sources(child)
                    elif isinstance(value, list):
                        for child in value:
                            bind_sources(child)
                # Bind only newly staged records; existing records without provenance stay unchanged.
                for collection in ("decisions", "tasks", "need_user"):
                    existing = {item["id"]: item for item in board[collection]}
                    for item in staged[collection]:
                        old = existing.get(item["id"])
                        if old is None:
                            bind_sources(item)
                        else:
                            for field in ("notes", "user_notes"):
                                for added in item.get(field, [])[len(old.get(field, [])):]:
                                    bind_sources(added)
                try:
                    self.store.save_board(self.gid, staged)
                except OSError as exc:
                    raise OSError("回复已保存为 #%s，但看板未保存：%s" % (message["seq"], exc)) from exc
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
            try:
                progress = self.store.complete_member_context(self.gid, member, seen_seq, context_state.get("deferred_from"))
            except Exception as exc:
                self._saved_warning("%s 的回复已保存为 #%s；已读进度未保存，下一轮可能重复提供未读全文：%s" % (name_of(member), message["seq"], exc))
            else:
                with self._lock:
                    self.seen[member] = progress["seen"]
            return True
        except Exception as exc:
            if message is not None:
                # Durable output is not a failed model call. Do not offer a retry
                # that would duplicate the reply or its committed side effects.
                self._saved_warning("%s 的回复已保存为 #%s，但后续保存未完成，请核对看板、候选记忆和已读进度：%s" % (name_of(member), message["seq"], exc))
                return True
            if isinstance(exc, StoreError):
                raise
            if not cancel.cancelled:
                self._failed_members.add(member)
            self._system("%s 这次未完成：%s" % (name_of(member), exc), retry_member=member, task=task)
            return False
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
        fold = prompts.bounded_summary_messages(group, board, fold)
        if not fold:
            self._system("摘要输入超过预算，尚未摘要的原文保留，摘要进度未推进。", subtle=True)
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
            if sec in self._failed_members or sec in self._paused_at_round_start or self.engine.availability.snapshot()[sec]["paused"]:
                continue
            with self._lock:
                self._speaking[sec] = now()
            route = None
            try:
                prompt = prompts.build_summary_prompt(group, board, fold, sec)
                query = next((m.get("text", "") for m in reversed(fold) if m.get("kind") == "user"), "")
                route, selected = self._select_model(sec, group, settings, "summary", query, prompt)
                route["quota_snapshot"] = self.engine.quota.get(sec, selected)
                if cancel.cancelled:
                    raise Cancelled()
                if self.engine.availability.snapshot()[sec]["paused"]:
                    continue
                adapter = self.engine.registry.adapter(sec, selected)
                raw = adapter.ask(prompt, self.store.workspace(self.gid), cancel)
                route.update(getattr(adapter, "last_metadata", {}))
            except Cancelled:
                return
            except (MemberError, ValueError) as exc:
                if cancel.cancelled:
                    return
                self._failed_members.add(sec)
                if isinstance(exc, MemberError):
                    self.engine.record_member_failure(sec, exc)
                self._system("%s 未能整理摘要：%s" % (name_of(sec), exc), subtle=True, model_route=route)
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
                board["summary_model_route"] = route
                board_mod.log(board, sec, "更新了前情摘要（截至 #%d）" % upto)
                self.store.save_board(self.gid, board)
                group = self.store.load_group(self.gid)
                group["secretary_index"] = group.get("secretary_index", 0) + 1
                self.store.save_group(group)
            self._system("%s 整理了前情摘要（截至 #%d），可以在看板里查看。" % (name_of(sec), upto), subtle=True, model_route=route)
            return
        self._system("这次前情摘要没有整理成功；本回合不自动重试失败成员，新的用户回合会再试。", subtle=True)

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
        availability = self.engine.availability.snapshot()
        if self._failed_members:
            self._system("本回合失败成员不自动重试：%s；失败发言可点“补答此成员”，摘要等新的用户回合再试。" % "、".join(name_of(m) for m in sorted(self._failed_members)), subtle=True)
        members = [m for m in group.get("members", []) if m in MEMBER_ORDER
                   and not availability[m]["paused"] and m not in self._failed_members]
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
        self.availability = AvailabilityStore(store.root)
        self.quota = QuotaMonitor(demo=getattr(registry, "demo", True))
        self._runners: dict = {}
        self._lock = threading.Lock()
        self._retry_lock = threading.Lock()

    def runner(self, gid: str) -> GroupRunner:
        with self._lock:
            existing = self._runners.get(gid)
        if existing is not None:
            return existing
        # Never hold the global runner lock while acquiring a group storage lock.
        progress = self.store.member_context(gid)
        with self._lock:
            if gid not in self._runners:
                self._runners[gid] = GroupRunner(self, gid, progress)
            return self._runners[gid]

    def state(self, gid: str) -> dict:
        state = self.runner(gid).snapshot()
        state["model_availability"] = self.availability.snapshot()
        state["incomplete_members"] = self.incomplete_members(gid)
        state["model_quota"] = self.quota.snapshot()
        return state

    def incomplete_members(self, gid: str) -> dict:
        pending = {}
        for message in self.store.messages(gid):
            if message.get("incomplete") and message.get("retry_member") in MEMBER_ORDER:
                pending[message["retry_member"]] = {"id": message["id"], "seq": message["seq"], "task": message.get("task", "reply")}
            elif message.get("kind") == "chat":
                pending.pop(message.get("author"), None)
        return pending

    def retry_member(self, gid: str, member: str) -> None:
        # Claim the pending failure once across concurrent HTTP requests. The
        # runner also checks idle atomically against normal message submission.
        with self._retry_lock:
            if member not in MEMBER_ORDER or member not in self.store.load_group(gid).get("members", []):
                raise ValueError("该成员不在群里")
            if self.availability.snapshot()[member]["paused"]:
                raise ValueError("请先核对额度并恢复该成员，再补答")
            if self.state(gid)["running"]:
                raise ValueError("请等当前讨论结束后再补答")
            if member not in self.incomplete_members(gid):
                raise ValueError("该成员没有待补的失败发言")
            self.runner(gid).submit({"type": "round", "participants": [member], "task": "continue", "trigger": None, "retry_only": True}, require_idle=True)

    def record_member_failure(self, member: str, error) -> None:
        kind = classify_cli_failure(error)
        if kind:
            self.availability.pause(member, kind, "CLI 报告额度不足或限流，已暂停新调用。请核对账号额度，恢复后手动启用；切换模型不保证恢复额度。")

    def post_user_message(self, gid: str, text: str) -> dict:
        mentioned = parse_mentions(text)
        # Resolve the runner before committing a message: loading progress may fail.
        runner = self.runner(gid)
        with self.store.lock(gid):
            members = self.store.load_group(gid).get("members", [])
            targets = [m for m in members if not mentioned or m in mentioned]
            msg = self.store.append_user_message(gid, text, targets)
        runner.submit({
            "type": "round", "participants": list(mentioned) if mentioned else None,
            "mentioned": list(mentioned or []), "task": "mention" if mentioned else "reply",
            "trigger": msg["seq"],
        })
        return msg

    def continue_discussion(self, gid: str) -> None:
        self.runner(gid).submit({"type": "round", "participants": None, "task": "continue", "trigger": None})

    def stop_existing(self, gid: str) -> bool:
        """Stop only an existing runner; safe even when group files are corrupt."""
        with self._lock:
            runner = self._runners.get(gid)
        return runner.stop() if runner is not None else False

    def stop(self, gid: str) -> bool:
        busy = self.stop_existing(gid)
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

    def model_preview(self, gid: str, text: str, task: str = "reply") -> dict:
        """Compute an estimate with saved configuration; no CLI, message or job is created."""
        if task not in ("reply", "mention", "parallel", "review", "continue", "vote", "debate", "summary"):
            raise ValueError("不支持的讨论阶段")
        with self.store.lock(gid):
            group = self.store.load_group(gid)
            settings = self.store.load_settings()
            board = self.store.load_board(gid)
            messages = list(self.store.messages(gid))
        if text:
            messages.append({"kind": "user", "author": "user", "text": text,
                             "seq": messages[-1]["seq"] + 1 if messages else 1})
        else:
            text = next((m.get("text", "") for m in reversed(messages) if m.get("kind") == "user"), "")
        memory = self.store.memory.context(group["project_id"], text, budget=6000)
        routes, errors = {}, {}
        for member in group.get("members", []):
            if member not in MEMBER_ORDER:
                continue
            progress = self.store.member_context(gid).get(member, {})
            prompt = (prompts.build_summary_prompt(group, board, prompts.bounded_summary_messages(group, board, messages), member) if task == "summary" else
                      prompts.build_member_prompt(group, board, messages, member, task, settings, memory_context=memory,
                          seen_seq=progress.get("seen"), unread_from=progress.get("unread_from")))
            try:
                routes[member] = resolve_route(member, settings, group, task, text, context_chars=len(prompt))
            except ValueError as exc:
                errors[member] = str(exc)
        return {"routes": routes, "errors": errors, "estimate": True}

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
            if "model_overrides" in changes:
                group["model_overrides"] = clean_overrides(changes["model_overrides"])
                for member in group["model_overrides"]:
                    resolve_route(member, self.store.load_settings(), group, "reply", "")
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
