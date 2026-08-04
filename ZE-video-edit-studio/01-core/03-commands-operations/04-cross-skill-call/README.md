# 04-cross-skill-call — 跨 skill 调用

> 一句话：A skill 调 B skill 的 command。

同步 / 异步语义 / 超时 / 错误传播 / trace 跨 skill 串联。

## 本叶子文件

- README.md — 本文件 (导览)
- SPEC.md — **已写(规则骨架)**:契约形状(同步/异步/超时/错误保真/trace 串联)+ 红线;**传输层/寻址/鉴权是开放设计决策,待 ZE 系列协同顶层拍板**
- impl + `GATE-XSK-*` 待传输层定稿(本轮不发明 transport)

## 怎么用

mod 作者要让 A skill 调 B skill 时，调用面按本叶子的契约形状写（参数语义见 SPEC §2）：

```python
call_skill(
    skill_id="ZE-seek-find-background-generator",  # 被调 skill
    command_id="bg.generate",   # 只能调 B 已注册导出的 command
    payload={...},
    actor=actor,                # 以谁的身份调
    mode="sync",                # "sync" 阻塞拿结果 / "async" 立即返 handle
    timeout_sec=30,             # sync 必带超时, 超时收 cross_skill_timeout
    trace_id=trace_id,          # 不传则继承当前 trace, 跨 skill 串成一条链
)
```

无论传输层选什么，调用代码都要守四条红线：不 `import` B 的 Python 模块（跨 skill 永远显式 `call_skill`）；B 的 `ZeError` code 原样传回 A（传输失败用 `cross_skill_*` code 族区分于 B 业务报错）；只调 B 经 `sources_allowed` ACL 导出的 command；`trace_id` 不断链。完整规则见 [SPEC.md](./SPEC.md)。
