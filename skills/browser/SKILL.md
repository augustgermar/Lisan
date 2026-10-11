---
name: browser
description: "Use when the owner has installed the managed browser skill and the task needs a persistent quiet or visible Chrome session, rendered web search, navigation, or human handoff."
version: 0.1.0
---

# Managed browser

This skill owns the platform-specific managed Chrome session. It is not part of
Lisan core. Install it when a user wants browser automation.

The executable `browser` tool provides two lanes:

- `quiet` (default): a headless persistent Chrome session for autonomous work;
- `loud`: the owner's visible Chrome window, used only when the owner should
  watch or act.

Use small actions and read after navigation. Ordinary reading, navigation,
tabs, and non-submitting fields do not require owner approval. Consequential
clicks and form submissions require a single-use owner receipt. A login or
CAPTCHA should use `handoff`; it returns immediately, and the owner can finish
with `handoff_finish`.

The skill includes its own browser implementation and approval receipt ledger.
Those files intentionally remain outside the core tool surface.
