---
id: TASK-2
title: 制定美术资产管理规范
status: Done
assignee: []
created_date: '2026-10-11 08:11'
updated_date: '2026-10-11 08:14'
labels:
  - art
  - scope
dependencies: []
priority: high
ordinal: 2000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
复用当前生产资产机制，明确资产、配置变体、视图、版本、同版预览和备选方案；规范 Owner 批准、精确交付与最小保留。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 规范与现有 CLI、定稿校验和清理机制一致
- [x] #2 设计获批、技术就绪和实机验收分别判断
- [x] #3 README 与 Agent 工作流能找到规范
<!-- AC:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
新增 docs/ART_ASSETS.md，并接入 README、AGENTS、CONTRIBUTING、AGENT_WORKFLOW。逐项核对 prod lock、--preview-of、--variant、--option、prod audit、prod cleanup-plan 与当前实现；保留三种独立验收，不增加新管理系统。
<!-- SECTION:FINAL_SUMMARY:END -->
