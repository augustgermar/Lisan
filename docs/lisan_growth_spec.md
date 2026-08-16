# Lisan Growth: Expanding the Knowledge Loop

This document outlines proposed architectural expansions to the **Ship 2** framework. The goal is to transition Lisan from a "reactive" agent (responding to deficits) to an "active" agent (seeking knowledge to resolve ambiguity) while strictly maintaining the "no-hoarding" principle.

## The Core Tension
*   **The Goal:** Allow the agent to proactively ask questions, conduct research, and refine its own understanding.
*   **The Risk:** The agent becomes a "vacuum cleaner," sucking in massive amounts of data to resolve trivialities, leading to a bloated, noisy vault.

To solve this, we propose moving from "Deficit-based" triggers to "Inquiry-based" cycles, governed by strict resource-cost-to-value ratios.

---

## 1. The Inquiry-Driven Loop (Self-Improvement)
Instead of only acting when an entity is "thin," the agent can generate **Inquiry-based Loops**.

*   **The Mechanism:** The agent identifies an "Ambiguity" (e.g., *"The current record of X is consistent but lacks the nuance to answer Y"*). It then creates an `inquiry_loop` record.
*   s**The Constraint:** An inquiry loop must be tied to a specific, high-level question or a "missing piece" of an existing story. It is not a "scan"; it is a "quest."
*   **The Value:** This allows the agent to "answer its own questions" by proactively seeking the missing evidence required to make a claim "solid."

## 2. The Evidence-Gated Write (Anti-Hoarding)
To prevent the "copy-paste" problem, we implement a strict **Claim-Evidence-Pointer** structure.

*   **The Mechanism:** The agent cannot write a "fact" to the vault unless it can provide a specific `evidence_link` (a pointer to a source) and a `claim_statement`.
*   **The Constraint:** The "Resolution" is the *only* thing that lands. If the agent finds a 50-page PDF, it only writes the one sentence that resolves the current loop.
*   **The Value:** This ensures the vault remains a collection of *knowledge*, not a collection of *documents*. The "corpus" stays in the source; the "truth" stays in the vault.

## 3. The Research Token Budget (Controlled Exploration)
To enable **Ring 2 (The Published World)** safely, we introduce a "Cost of Curiosity."

*   **The Mechanism:** The agent is granted a small, periodic budget of **Research Tokens** (e.g., 5 tokens per week). 
*   **The Constraint:** A Ring 2 search *costs* a token. The agent must "spend" a token to initiate an external web-search loop. s
*   **The Value:** This forces the agent to prioritize. It won't go to the web for a triviality if it has a "high-value" question waiting. It makes the "cost of curiosity" an explicit part of the agent's own decision-making.

## 4. The Decay & Supersede Mechanism (Maintaining Signal-to-Noise)
To prevent "stale" knowledge from cluttering the vault, we implement an **Entropy-based Lifecycle**.

*   **The Mechanism:** Every `inference` or `unverified_claim` has a "half-life." As time passes without the claim being reinforced by "direct_evidence," its confidence score decays.
*   **The Constraint:** When a claim's confidence falls below a certain threshold, it is "tombstoned" (archived) or flagged for a "re-evaluation loop."
*   **The Value:** This ensures that the "active" memory of the agent is always the most reliable. It prevents old, outdated guesses from masquerading as current facts.

---

## Summary of the "Active" Architecture

| Feature | Purpose | Safety Guard |
| :--- | :--- | :--- |
| **Inquiry Loops** | Proactive self-improvement | Must be tied to a specific, named ambiguity. |
| **Evidence-Gated Writes** | Prevents data-hoarding | Only the *resolution* is written, never the *corpus*. |
| **Research Tokens** | Controlled web-exploration | A hard budget that prevents "infinite" web-crawling. |
| **Entropy Decay** | Maintains high signal-to-noise | Old/unverified claims naturally recede to the background. |
