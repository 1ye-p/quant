# 上游 issue 草案：factor-research SKILL.md 正交化章节过时（F5）

> 状态：**草案（架构侧起草，用户提交）**。提交后回填 issue 链接至
> `docs/PRD_REGRESSION_REDLINE_CLOSEOUT.md` §F5。
> 目标仓库：vibe-trading（本仓子模块 `lib/vibe-trading`）。
> 参照位置：`agent/src/skills/factor-research/SKILL.md:95-103`（截至 v0.1.13-165-g4ad3b6b0）。

---

## Title

factor-research SKILL.md: "Orthogonalized Combination" section references a workflow that no longer exists in the cQuant bridge

## Body

### Summary

The `factor-research` skill's SKILL.md still documents an **"Orthogonalized Combination"**
section (lines ~95-103): orthogonalize factors with the Schmidt process to remove
collinearity, then combine with equal weights.

The cQuant bridge (`qlib_bridge`) has **removed this path entirely** (the standalone
`orthogonalize.py` was deleted). Multi-factor combination now goes through
`CrossSectionScorer._neutralize_factors` (residual projection) instead. Following the
section as written leads to calls against a backend path that no longer exists.

### Details

- Section: `agent/src/skills/factor-research/SKILL.md` → `### Orthogonalized Combination`
- It instructs: sort factors by IC, regress later factors on earlier ones, use residuals
  as orthogonalized factors, combine with equal weights.
- Downstream impact: AI agents reading SKILL.md are guided toward a removed code path and
  fail at runtime; downstream docs have already been annotated
  (see cQuant `KNOWN_ISSUES.md` §3).

### Request

Either:

1. Delete the "Orthogonalized Combination" section, **or**
2. Mark it as deprecated with a pointer to the residual-projection neutralization flow
   (`CrossSectionScorer._neutralize_factors`) for consumers that implement it downstream.

Option 1 is preferred if no other consumers rely on the Schmidt path.

### Environment

- vibe-trading @ v0.1.13-165-g4ad3b6b0 (submodule pointer observed 2026-10-07)
