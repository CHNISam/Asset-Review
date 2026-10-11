---
id: TASK-6
title: 补齐异常输入与请求预算覆盖
status: To Do
assignee: []
created_date: '2026-10-11 08:11'
labels:
  - security
dependencies: []
priority: low
ordinal: 6000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
迁自原 ROADMAP P0：已有路径／Origin／SVG／权限等回归。尚未确认 fuzz、损坏图片和请求预算覆盖是否足够，开工前核对当前测试。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 以当前测试识别缺口，避免重复覆盖
- [ ] #2 对确有缺口验证坏输入可被有界拒绝且正常输入通过
<!-- AC:END -->
