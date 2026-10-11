---
id: TASK-1
title: 接入 Backlog.md 工作项管理
status: Done
assignee: []
created_date: '2026-10-11 08:09'
updated_date: '2026-10-11 08:13'
labels:
  - scope
dependencies: []
priority: high
ordinal: 1000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
以 backlog/tasks 为 OpenCrit 唯一工作项状态来源；入口与协作说明可发现，原 Roadmap 不再维护并行待办。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Backlog CLI 能读取工作项并显示看板
- [x] #2 README、AGENTS、CONTRIBUTING 指向同一工作项来源
- [x] #3 现有未完成方向迁入 backlog，延期事项不默认开工
<!-- AC:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
已通过 Backlog.md 1.53.0 官方 CLI 初始化并创建／读取工作项。backlog/tasks 为唯一状态源，ROADMAP 改为方向入口；旧未完成方向迁入 TASK-3、5–9。关闭自动提交、远端操作和跨分支扫描，避免后台流量。
<!-- SECTION:FINAL_SUMMARY:END -->
