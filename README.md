# task26100494

离线 Newsletter 受众封版与派发确认库。纯 Python 标准库实现（`sqlite3`），
无外部依赖、无网络、无监听端口，数据与确认正文均存储在单个 SQLite 文件中。
本库的"派发"仅指本地 sent-marked 记录，不实际发送邮件。

## 能力概览

`NewsletterService` 是上层调用的唯一入口，面向单一出版者，提供四类能力：

- **订阅者变更** `service.roster`
  - `subscribe(id, tier)` / `unsubscribe(id)` / `set_tier(id, tier)`
  - 每次成功变更返回严格递增的版本号；无变化的调用返回当前版本、不产生事件。
  - 重新订阅沿用原 ID；`tier` 为 `free` 或 `paid`。
- **刊期封版** `service.issues.seal(content, target_tier) -> issue_id`
  - 在单个事务内选定名册版本并快照目标，目标恰好是该版本下活跃且满足
    等级条件的全部订阅者（`paid` 仅付费；`free` 含全部活跃订阅者）。
  - 封版后的新增订阅与等级调整不影响本期目标。
- **推进派发** `service.issues.advance(issue_id, generator)`
  - `generator(content, target_id) -> str` 为调用方提供的进程内回调。
  - 封版边界之后发生过退订的目标被永久抑制，重新订阅也不恢复本期资格；
    生成期间发生退订的，迟到正文被丢弃并转为抑制。
  - 生成异常不产生确认、目标保持 pending，后续推进可重试。
  - 全部目标进入 confirmed / suppressed 终态后刊期 complete，之后推进为 no-op。
- **读取刊期结果** `service.issues.result(issue_id) -> IssueResult`
  - 返回封版版本、刊期状态、各目标状态与已确认正文；
    pending / suppressed 目标不暴露正文。

## 使用示例

```python
from newsletter import NewsletterService

with NewsletterService("newsletter.sqlite3") as svc:
    svc.roster.subscribe("alice", "paid")
    svc.roster.subscribe("bob", "free")

    issue_id = svc.issues.seal("第 42 期", "paid")
    svc.issues.advance(issue_id, lambda content, uid: f"{content}，{uid} 收")

    result = svc.issues.result(issue_id)
    for t in result.targets:
        print(t.subscriber_id, t.status, t.body)
```

## 运行测试

```sh
python3 -m unittest discover -s tests -v
```

## 代码结构

- `newsletter/db.py` — SQLite schema 与连接初始化
- `newsletter/roster.py` — 名册变更与版本号（`roster_events` 事件溯源）
- `newsletter/issues.py` — 封版快照、推进确认 / 抑制、结果查询
- `newsletter/service.py` — 门面，组合以上能力
- `tests/test_newsletter.py` — 自动化测试（含生成期间退订重订阅、
  异常重试与终态唯一性等场景）
