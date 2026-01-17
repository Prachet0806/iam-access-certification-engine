#lambdas/ai_explanation/handler.py
import os
import json
import sys
from pathlib import Path

from google import genai

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import repo
from common.db import db
from common import logger

GENAI_MODEL = "gemini-3-flash-preview"
API_KEY = os.getenv("GOOGLE_API_KEY")
client = genai.Client(api_key=API_KEY) if API_KEY else None

SYSTEM_PROMPT = (
    "You are an Identity Governance and Compliance Analyst.\n\n"
    "Given:\n- A user's department and role\n- An IAM policy in JSON format\n\n"
    "Explain in ONE concise sentence:\n"
    "1. Why this access is risky (if it is)\n"
    "2. What action is recommended\n\n"
    "Do NOT make final decisions.\n"
    "Do NOT invent facts.\n"
    "Use clear, non-technical language.\n"
    "Return plain text only.\n"
)


def generate_ai_summary(user_context: dict, policy_json: dict) -> str:
    if not client:
        raise RuntimeError("GenAI client not initialized")

    prompt = (
        f"User Context:\n{json.dumps(user_context, indent=2)}\n\n"
        f"IAM Policy:\n{json.dumps(policy_json, indent=2)}\n"
    )

    response = client.models.generate_content(
        model=GENAI_MODEL,
        contents=f"{SYSTEM_PROMPT}\n\n{prompt}",
        config={"temperature": 0.0},
    )

    if hasattr(response, "text") and response.text:
        return response.text.strip()

    raise ValueError("Empty GenAI response")


def _existing_ai_summary(conn, review_id: str) -> str | None:
    cur = conn.cursor()
    db.execute(
        cur,
        """
        SELECT ai_risk_summary
        FROM access_reviews
        WHERE review_id = ?
        """,
        (review_id,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _build_context_from_db(conn, review_id: str) -> tuple[dict, dict, str] | None:
    row = repo.fetch_review_context(conn, review_id)
    if not row:
        return None
    _, user_id, role_id, user_name, role_name, risk_level = row
    user_context = {
        "user_id": user_id,
        "user_name": user_name,
        "department": "Unknown",
        "role": role_name,
    }
    policy_json = {"policy_arn": role_id, "policy_name": role_name}
    return user_context, policy_json, risk_level


def _persist_summary(conn, review_id: str, summary: str):
    db.execute(
        conn.cursor(),
        """
        UPDATE access_reviews
        SET ai_risk_summary = ?
        WHERE review_id = ?
        """,
        (summary, review_id),
    )


def _process_single_review(conn, review_id: str, user_context: dict | None, policy_json: dict | None):
    existing = _existing_ai_summary(conn, review_id)
    if existing:
        logger.log("ai_explanation", "skip", "Already present", entity_id=review_id)
        return {"status": "SKIPPED", "review_id": review_id, "reason": "already_present"}

    risk_level = "HIGH"
    if not user_context or not policy_json:
        context = _build_context_from_db(conn, review_id)
        if not context:
            logger.log("ai_explanation", "error", "Review not found", level="ERROR", entity_id=review_id)
            return {"status": "FAILED", "review_id": review_id, "reason": "not_found"}
        user_context, policy_json, risk_level = context

    if risk_level != "HIGH":
        logger.log("ai_explanation", "skip", "Non-high risk", entity_id=review_id)
        return {"status": "SKIPPED", "review_id": review_id, "reason": "non_high_risk"}

    try:
        summary = generate_ai_summary(user_context, policy_json)
    except Exception as e:
        logger.log("ai_explanation", "warn", f"AI explanation failed: {e}", level="WARN", entity_id=review_id)
        summary = (
            "High-risk access detected based on policy and role mismatch. "
            "Manual review recommended."
        )

    _persist_summary(conn, review_id, summary)
    logger.log("ai_explanation", "success", "AI explanation stored", entity_id=review_id)
    return {"status": "SUCCESS", "review_id": review_id}


def handler(event, context):
    event = event or {}
    review_id = event.get("review_id")
    user_context = event.get("user_context")
    policy_json = event.get("policy_json")

    if not client:
        logger.log("ai_explanation", "skip", "GOOGLE_API_KEY not set; AI disabled")
        return {"status": "DISABLED", "reason": "missing_api_key"}

    with db.get_connection() as conn:
        if review_id:
            logger.log("ai_explanation", "start", "AI explanation started", entity_id=review_id)
            return _process_single_review(conn, review_id, user_context, policy_json)

        logger.log("ai_explanation", "start", "Batch AI explanation for HIGH risk")
        results = []
        for row in repo.list_high_risk_reviews_missing_ai(conn):
            r_id = row[0]
            results.append(_process_single_review(conn, r_id, None, None))
        return {"status": "SUCCESS", "processed": results}


if __name__ == "__main__":
    handler(None, None)
