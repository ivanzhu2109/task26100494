# task26100494

离线 Newsletter 受众封版与派发确认库（Python + SQLite，单出版者，由上层直接调用）。
数据与确认正文均存于 SQLite；“派发”仅指本地 sent-marked 记录，不实际发送邮件。

## 结构

- `newsletter/library.py` — 全部实现：`NewsletterLibrary` 及 `Tier` / `TargetStatus` / `IssueInfo` / `TargetResult` 类型。
- `tests/test_newsletter.py` — 自动化测试（标准库 `unittest`，无第三方依赖）。

## 运行测试

```sh
python3 -m unittest discover -s tests -v
```

要求：Python 3.9+（开发验证于 3.14），仅使用标准库 `sqlite3`。不需要 Docker、数据库服务或任何监听端口的进程。

## API 概览

```python
from newsletter import NewsletterLibrary, Tier

lib = NewsletterLibrary("newsletter.sqlite")   # 或 ":memory:"

# 1) 订阅者变更：每次成功变更返回严格递增的版本号；重新订阅沿用原 ID
v = lib.subscribe("alice", Tier.PAID)
lib.set_tier("alice", Tier.FREE)
lib.unsubscribe("alice")
lib.subscribe("alice", Tier.PAID)              # 重新订阅

# 2) 刊期封版：返回所选名册版本；目标冻结为该版本下活跃且满足等级的全部订阅者
sealed_version = lib.seal_issue("issue-42", "本期固定内容", Tier.PAID)

# 3) 推进派发：generator(content, subscriber_id) -> body，可能抛异常（可重试）
lib.advance("issue-42", lambda content, sid: f"{content} / {sid}")

# 4) 读取刊期结果：封版目标、各目标状态（pending/confirmed/suppressed）、已确认正文
info = lib.get_issue("issue-42")
```

## 语义要点

- 封版在单个 IMMEDIATE 事务内选版本并快照目标，不混合多个版本的记录；封版后的新增订阅与等级调整不影响本期目标。
- 未确认目标只要在封版边界之后发生过退订即被永久抑制，重新订阅不恢复本期资格；退订前已提交的确认保留。
- 正文生成成功后再次检查抑制条件：生成期间发生退订的，迟到的正文不进入确认结果。
- 生成异常不产生确认、目标保持 pending，可在后续 `advance` 重试；同一期同一目标只有一份已确认正文。
- 全部目标进入 confirmed/suppressed 终态后刊期完成，之后 `advance` 不再改变结果。
