"""Deterministic demonstration in a temporary store, not an AI-quality benchmark."""
import json
import tempfile
from memory import Memory


def main():
    with tempfile.TemporaryDirectory(prefix="correction-demo-") as directory:
        with Memory(directory) as mem:
            task = mem.begin_task("demo-video-chat")["task"]
            permanent = mem.record({
                "key": "writing.answer-order", "title": "先给结论", "desired": "先给结论，再解释依据。",
                "checks": ["第一段是否直接回答问题？"], "evidence": "以后都先给我结论"
            }, scope="global", consent_quote="以后都先给我结论")
            current = mem.record({
                "key": "video.direct-viewing", "title": "连续观看不能被抽帧代替",
                "desired": "寻找当前模型直接接收连续视频的办法；找不到就提供可核查依据。",
                "rejected": ["把抽帧自动化再次当成连续观看的解决方案", "改用其他模型代看"],
                "checks": ["是否把已拒绝的替代方案换名后再推荐？", "可行性结论是否有输入接口支持的依据？"],
                "domains": ["video"], "evidence": "我最想要的是能够实现你能通过连续观看我给你的视频这个点而不是一帧一帧的去抽画面出来看"
            }, scope="task", session="demo-video-chat", task=task)
            same_chat = mem.prepare("demo-video-chat", task=task)
            next_chat = mem.prepare("demo-writing-chat")
            checks = mem.review("可以开发后台自动抽帧工具来完成你的连续观看要求。", "demo-video-chat", task=task)
            assessed = mem.assess(checks["run_id"], [
                {"rule_id": r["id"], "status": "violated" if r["id"] == current["id"] else "met",
                 "evidence": "草稿把自动抽帧再次作为连续观看的替代方案。" if r["id"] == current["id"] else "草稿直接给出其结论。"}
                for r in same_chat["rules"]])
            mem.set_enabled(current["id"], 1, False)
            mem.undo(current["id"], 2)
            mem.finish_task("demo-video-chat", task)
            after_task = mem.prepare("demo-video-chat", task=task)
            print(json.dumps({
                "demonstration_only": True,
                "same_chat_rules": [{"title": r["title"], "scope": r["scope"]} for r in same_chat["rules"]],
                "next_chat_rules": [{"title": r["title"], "scope": r["scope"]} for r in next_chat["rules"]],
                "review": checks,
                "assessment_stage": assessed["stage"],
                "after_task_rules": [{"title": r["title"], "scope": r["scope"]} for r in after_task["rules"]],
                "restored_rule_revision": mem.get(current["id"])["revision"],
                "notice": "These are tool outputs, not a semantic pass. The current model must recognize the rejected substitute. No real user database was changed."
            }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
