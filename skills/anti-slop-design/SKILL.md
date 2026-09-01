---
name: anti-slop-design
description: "Automatically route every new or restyled UI through the anti-slop design stack before delivery. Use whenever generating, styling, redesigning, or materially editing a frontend, landing page, dashboard, app screen, component, deck-like web artifact, or design system, even when the user does not mention AI slop."
version: 1.0.0
author: Ricardo Alanis
license: MIT
---

# Anti-slop design router

Apply this workflow automatically to UI work. The user does not need to ask for an
anti-slop pass. This router chooses the smallest relevant frontier from `hallmark`,
`unslop-ui`, and `avoid-ai-design`; it does not blindly run three overlapping rewrites.

## Route before generating

1. **New UI or page:** load `hallmark` and use its build flow. Before writing code,
   also load `unslop-ui` and establish its concrete reference, color, type, and layout
   decisions. A vague "modern and clean" brief is not enough.
2. **Existing UI, visual cleanup:** load `avoid-ai-design` in rewrite mode. Preserve
   behavior, accessibility, routes, data flow, copy meaning, and existing brand tokens.
3. **Structural redesign:** load `hallmark` in redesign mode plus `unslop-ui`. State the
   files and boundaries before editing; deletions still require explicit approval.
4. **Audit-only request:** use `unslop-ui`'s scanner first, then the audit/detect mode of
   `hallmark` or `avoid-ai-design` that best fits the scope. Do not edit.

Use the host's native skill mechanism to load each named skill. If the host has no skill
loader, read that installed skill's complete `SKILL.md` and only the references it routes
to. Map host-specific tool names to the closest native equivalent; preserve every safety
and approval gate.

## Required generation gate

Before delivering any generated or materially restyled UI:

1. Render it when a browser or preview surface exists.
2. Run `unslop-ui`'s deterministic scanner when the stack is supported; otherwise apply
   its ranked audit manually and label visual-only judgments as inferred.
3. Check the result against `avoid-ai-design` for P0 tells. No purple-to-blue default
   gradient, untouched shadcn theme, reflexive glassmorphism, generic centered hero plus
   three cards, or default type choice survives unless it is an explicit brand decision.
4. Re-check responsive behavior and interaction states required by `hallmark`.
5. Fix failures before delivery. Report the chosen direction and the audit result in two
   short lines; do not make the user request a separate cleanup pass.

## Boundaries

- Existing brand and design-system constraints outrank aesthetic novelty.
- Never replace one default with another recurring house style.
- Never invent metrics, testimonials, logos, or product claims to fill a layout.
- Do not install dependencies, publish, post, or delete files without the authorization
  required by the active host and project.
