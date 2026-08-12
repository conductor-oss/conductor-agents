"""Pure decision logic for pr_review's private investigation loop.

See `common/gate_decision.py` for the gate-decision resolver and the review
comment summarizer this module reuses.
"""

from __future__ import annotations

from . import gate_decision


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _blank(value: object) -> bool:
    return not _text(value).strip()


def _num(value: object) -> float | int:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _listed(value: object) -> list:
    return value if isinstance(value, list) else []


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def normalize_investigation(*, structured: object, status: object, error: object,
                            session_id: object, prior_session_id: object, question: object,
                            prior_review: object, history: object, prior_tokens: object,
                            tokens: object, prior_cost: object, cost: object) -> dict:
    """Fold one investigation turn into the running answer/review/history.

    A failed or empty-answer turn keeps the prior review rather than replacing
    it with nothing: an investigation is optional evidence-gathering, and it
    must never blank out a review that already exists.
    """
    result = _mapping(structured)
    answer = result.get("answer")
    succeeded = status == "success" and isinstance(answer, str) and not _blank(answer)
    if succeeded:
        resolved_answer = answer
    else:
        detail = f": {error}" if isinstance(error, str) and error else \
            "; the previous review was preserved."
        resolved_answer = f"Investigation could not complete{detail}"

    if succeeded and isinstance(result.get("review"), dict):
        review = gate_decision.summarize_review(result["review"])
    else:
        review = prior_review

    if isinstance(session_id, str) and session_id:
        resolved_session = session_id
    elif isinstance(prior_session_id, str):
        resolved_session = prior_session_id
    else:
        resolved_session = ""

    prior_history = _listed(history)
    entry = {"pass": len(prior_history) + 1, "question": question,
             "answer": resolved_answer, "status": "success" if succeeded else "failed"}
    return {
        "answer": resolved_answer, "review": review, "sessionId": resolved_session,
        "history": prior_history + [entry], "count": len(prior_history) + 1,
        "tokenUsed": _num(prior_tokens) + _num(tokens),
        "costUsd": _num(prior_cost) + _num(cost),
    }
