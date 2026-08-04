# 批量写盘 Checkpoint 合同(权威文档;Grok-Build 借鉴 P1-⑥ 第一步,2026-07-19)

> 状态:**语义已定,实现未落**(Extension Path 正序:权威文档 → pipeline 实现 → verify 门禁)。
> 实现的业务拉动点=批量生图/隐物资产类「AI 大批量直接写盘」pipeline(OpLog 域外产物)。
> 在实现落地前,本文档是唯一真源;任何先行实现必须回来对齐本合同。

## 为什么需要(与 OpLog 的关系)

OpLog undo/redo 在「走 command 总线的动作」域内完备(operation 粒度、跨重启、reverse patch)。
盲区在总线之外:AI pipeline 批量落盘的产物(图片批次、资产包)不产生 operation——一批错了,
没有「整批回滚」的把手。Checkpoint 是**正交的物理层防线**:OpLog 管语义,checkpoint 管现场。
同一安全哲学的既有先例:Trash Guard(删除前备份)。

## 语义(v1 冻结)

- **粒度**:一个 pipeline 批次(run)= 一个 checkpoint;批次开始前对**目标写盘区**做快照。
- **目标写盘区**:批次声明将写入的目录集(StageSpec 声明或 supervisor 推导);
  永不包含 `02-mod/`、`.data/` 之外的区域快照不得触碰受管层。
- **恢复**:失败或人工否决 → 整批恢复 = 快照区回放 + 批内新增文件删除;恢复本身记一条
  `source="system"` 的说明性 Operation(可见、可审计,不可再 undo)。
- **证据**:`ZE_BUILD_EVIDENCE` 不涉;运行证据落 pipeline run 记录:
  `checkpoint: {run_id, created_at, paths: [...], bytes, restored: bool|null, retained_until}`。
- **成本诚实**(学 grok 的姿态):快照是磁盘大头——默认保留期 72h,过期自动清;
  文档与 UI 明示成本,不隐藏。
- **降级**:快照失败 → 批次照跑但 run 记录 `checkpoint: {error}`(warn 级),不阻断生产。

## 实现清单(未做,按序)

1. supervisor 批次入口:快照(硬链/复制两级降级;macOS 可用 APFS clonefile 加速,记录在案不强制)
2. 恢复命令(native,`requires_confirm`,sources_allowed 含 ai——恢复本身也要走确认闸门)
3. verify 门禁:声明了批量写盘的 pipeline 必须带 checkpoint 声明
4. 保留期清理挂 maintenance
