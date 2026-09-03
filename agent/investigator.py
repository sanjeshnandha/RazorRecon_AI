"""
The investigation agent.

What it is: a language model given read-only tools over one finished
reconciliation run, asked to explain the residue in English.

What it is emphatically not: part of the reconciliation. Every rupee, every
tier and every match rate was computed deterministically by the engine before
this module is ever imported. The agent reads those results. It cannot write,
recompute, or reclassify anything -- there is no code path from here to a
mutation, because agent/tools.py contains only SELECTs.

That separation is the point. A number you can prove and an explanation you can
read are different products, and blending them would cost you the first one.

Two guardrails run after the model answers:

  * every settlement, delta, exception and record id the answer mentions is
    checked against the ids that actually appeared in tool results. Anything
    else is reported as an unsupported reference rather than quietly shipped.
  * the tool-call budget is bounded, so a confused model stops rather than
    grinding through the dataset.
"""
from __future__ import annotations

import json
import re
import time

from agent import tools as toolmod
from agent.llm import LLMClient, LLMUnavailable

MAX_ITERATIONS = 8          # assistant turns; each may carry several tool calls
MAX_TOOL_CALLS = 20
MAX_TOOL_RESULT_CHARS = 20_000

# Record-id shapes used across the schema. Used only to VERIFY citations, never
# to parse meaning out of the answer.
ID_PATTERN = re.compile(
    r"\b("
    r"SET_\d+(?::D[1-4])?"      # settlements and delta ids
    r"|EXC_\d+|ATT_\d+|GT_\d+"  # exceptions, attributions, ground truth
    r"|P_\d+|R_\d+|A_\d+|T_\d+|O_\d+|C_\d+|S_\d{3,}"
    r"|ADJ_\d+|SI_\d+|B_\d+|L_\d+|G_\d+"
    r")\b")

SYSTEM_PROMPT = """\
You are the investigation agent for a deterministic settlement reconciliation \
engine. A batch has already been reconciled. Your job is to explain what the \
engine found, in plain English, to a finance controller.

WHAT YOU ARE NOT
You did not compute any of these numbers and you must never recompute them. The \
engine derived every figure from a versioned policy registry using integer paise \
arithmetic, and its results are already persisted. You have read-only tools over \
those results. You cannot change a number, a tier, or an exception's status, and \
you must never imply otherwise or promise to fix, resolve or update anything.

HOW TO WORK
1. Call tools before answering. Never answer a factual question from memory.
2. Quote figures exactly as the tools return them. Use the *_display rupee \
strings when writing amounts. Do NOT do arithmetic yourself -- if a number you \
want is not in a tool result, say it is not available rather than deriving it.
3. Cite the records you relied on by id: settlement ids (SET_0099), delta ids \
(SET_0099:D2), exception ids (EXC_00012), and the rule ids that appear in \
attribution evidence (POLICY.MDR.CARD@1.0.0). Only cite ids you actually saw in \
a tool result.
4. When the evidence does not settle the question, say so explicitly and say \
what would settle it. "The matcher found no candidate bank line, so I cannot \
tell from this run whether the credit is late or missing" is a good answer. \
Inventing a cause is not.

WHAT THE FOUR DELTAS MEAN
D1_COMPUTE  the net recomputed from policy vs the net the settlement report claims
D2_BANK     that net vs the bank credit that actually arrived
D3_LEDGER   the merchant's own double-entry books for those payments
D4_PAYOUT   what each seller was owed vs what was actually transferred
They are reported separately and never blended. A settlement can be perfect on \
D1 and badly wrong on D4.

TIERS
A auto-resolved on deterministic evidence. B needs human review. C unresolved -- \
the engine refused to guess. Tier C is a deliberate outcome, not a failure: the \
engine declining an ambiguous match is the behaviour that makes the other \
numbers trustworthy. Explain it that way.

POLICY
All rates come from a synthetic "Demo Merchant Policy" authored for this project. \
It is NOT Razorpay's real commercial pricing. If you cite a rate, take it from \
get_policy and never present it as a real-world Razorpay term.

STYLE
Answer in a few short paragraphs. Lead with the answer, then the evidence. No \
preamble, no bullet-point dumps of raw tool output, no headings unless the \
question genuinely needs them."""


def _extract_ids(text: str) -> set[str]:
    return set(ID_PATTERN.findall(text or ""))


def _ids_in(obj) -> set[str]:
    """Every id-shaped token anywhere in a tool result, at any depth."""
    return _extract_ids(json.dumps(obj, default=str))


def investigate(conn, run_id: str, dataset_id: str, question: str,
                history: list[dict] | None = None, client: LLMClient | None = None) -> dict:
    """Run one question to completion. Returns the answer plus everything needed
    to audit how it was reached."""
    client = client or LLMClient()
    ctx = {"run_id": str(run_id), "dataset_id": str(dataset_id)}

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in (history or [])[-6:]:            # keep the last three exchanges
        role = turn.get("role")
        if role in ("user", "assistant") and turn.get("content"):
            messages.append({"role": role, "content": str(turn["content"])[:4000]})
    messages.append({"role": "user", "content": question})

    trail: list[dict] = []
    evidence_ids: set[str] = set()
    started = time.perf_counter()
    stop_reason = "answered"
    answer = ""

    for _ in range(MAX_ITERATIONS):
        budget_left = MAX_TOOL_CALLS - len(trail)
        msg = client.complete(messages,
                              tools=toolmod.SCHEMAS if budget_left > 0 else None)
        calls = msg.get("tool_calls") or []
        # Some providers return content alongside tool calls; keep both so the
        # transcript reflects what actually came back.
        messages.append({"role": "assistant",
                         "content": msg.get("content") or "",
                         **({"tool_calls": calls} if calls else {})})

        if not calls:
            answer = (msg.get("content") or "").strip()
            break

        for call in calls[:budget_left]:
            fn = (call.get("function") or {})
            name = fn.get("name") or ""
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                args = {}
                result = {"error": f"arguments for {name} were not valid JSON: {raw!r:.200}"}
            else:
                result = toolmod.dispatch(conn, ctx, name, args)

            evidence_ids |= _ids_in(result)
            blob = json.dumps(result, default=str)
            if len(blob) > MAX_TOOL_RESULT_CHARS:
                blob = blob[:MAX_TOOL_RESULT_CHARS] + '..."TRUNCATED"}'
            trail.append({"tool": name, "arguments": args, "result_chars": len(blob)})
            messages.append({"role": "tool", "tool_call_id": call.get("id") or name,
                             "name": name, "content": blob})

        if len(trail) >= MAX_TOOL_CALLS:
            stop_reason = "tool budget exhausted"
            messages.append({"role": "user", "content":
                             "You have used your full tool budget. Answer now from what you "
                             "already retrieved, and say plainly which parts you could not "
                             "verify."})
    else:
        stop_reason = "iteration limit reached"

    if not answer:
        # The loop ran out without a final message; ask once for a plain answer.
        try:
            final = client.complete(messages, tools=None)
            answer = (final.get("content") or "").strip()
        except LLMUnavailable:
            answer = ""
        if not answer:
            answer = ("I could not complete this investigation within the tool budget. "
                      "Try a narrower question, for example about one settlement id.")

    cited = _extract_ids(answer)
    unsupported = sorted(cited - evidence_ids)
    return {
        "answer": answer,
        "citations": sorted(cited & evidence_ids),
        # An id in the answer that never appeared in any tool result is the one
        # failure mode that matters here: a plausible-sounding reference to a
        # record that was never read. Surfaced, never suppressed.
        "unsupported_references": unsupported,
        "grounded": not unsupported,
        "tool_calls": trail,
        "tool_call_count": len(trail),
        "stop_reason": stop_reason,
        "model": client.config.model,
        "provider": client.config.label,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }


SUGGESTED_QUESTIONS = [
    "What is the single largest unexplained amount in this run, and why could the engine not resolve it?",
    "Which settlements are unresolved (tier C), and what do they have in common?",
    "Walk me through the biggest D1 compute difference. Which rule was violated?",
    "Are any sellers being underpaid? Show me the worst case.",
    "Why did the bank matcher refuse to match rather than picking the closest candidate?",
    "Summarise this run for a CFO in four sentences.",
]
