---
id: TASK-4
title: 收口音频延期并保护旧数据
status: Done
assignee: []
created_date: '2026-10-11 08:11'
updated_date: '2026-10-11 08:18'
labels:
  - scope
dependencies: []
priority: high
ordinal: 4000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
当前仅保留美术评审。移除音频扫描、上传、播放、波形、专用类型及入口；保留已有文件和数据库记录，恢复方向另列延期任务。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 音频扩展名不扫描、不上传、不提供原文件
- [x] #2 音频专用 UI、脚本、波形端点和生产类型移除
- [x] #3 旧数据保留，图片和视频回归通过
<!-- AC:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
复用 main 的 c01d10b 音频移除与 5ea3a9d 单候选定稿修复；不重复移除、不覆盖现有 UI。新增兼容保护：旧音频记录退出画廊、生产清单、分配菜单、整理计划及直接操作，数据库历史和原文件保留。新增范围／旧数据回归；最终验证见 PR。发布依赖审计通过。Bandit 有 preview_worker.py 中 3 条既有低级子进程告警；本地浏览器下载失败，PR 浏览器 CI 待运行。
<!-- SECTION:FINAL_SUMMARY:END -->
