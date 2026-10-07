import logging
import os
import httpx
from typing import Any

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)


MISTAKE_CATEGORIES = {
    "arithmetic_error": (
        "The student chose an appropriate method but made a numerical "
        "calculation or basic arithmetic mistake."
    ),
    "conceptual_error": (
        "The student misunderstands an important mathematical concept."
    ),
    "procedural_error": (
        "The student understands the general concept but applies a rule, "
        "formula, or sequence of steps incorrectly."
    ),
    "misread_problem": (
        "The student misunderstood the wording, quantities, conditions, "
        "or what the problem was asking."
    ),
    "insufficient_evidence": (
        "The available answer and reasoning do not contain enough information "
        "to identify the mistake reliably."
    ),
}


async def classify_with_jev(
    problem: str,
    student_answer: str,
    student_reasoning: str,
) -> dict[str, Any]:
    """Send the student's work directly to the Jev API."""

    api_key = os.getenv("TYPESAFE_API_KEY")
    if not api_key:
        raise RuntimeError("TYPESAFE_API_KEY is not configured")

    request_body = {
        "model": "jev-latest",
        "state": {
            "math_problem": problem,
            "student_answer": student_answer,
            "student_reasoning": student_reasoning,
        },
        "questions": {
            "mistake_category": {
                "type": "choice",
                "instructions": (
                    "Classify the primary reason the student's answer is "
                    "incorrect. Use only the supplied problem, answer, and "
                    "reasoning."
                ),
                "criteria": MISTAKE_CATEGORIES,
            }
        },
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://api.typesafe.ai/v1/systemone",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=request_body,
        )
        response.raise_for_status()

    response_data = response.json()
    answer = response_data["answers"]["mistake_category"]

    probabilities = {
        category: round(float(probability), 4)
        for category, probability in answer["probabilities"].items()
    }

    ranked = sorted(
        probabilities.items(),
        key=lambda item: item[1],
        reverse=True,
    )

    top_probability = ranked[0][1]
    second_probability = ranked[1][1]

    return {
        "category": answer["choice"],
        "confidence": float(answer["confidence"]),
        "probabilities": probabilities,
        "needs_clarification": (
            top_probability < 0.65
            or top_probability - second_probability < 0.15
        ),
    }

class DiagnoseMathWork(Tool):
    """Classify the likely mistake in a student's math reasoning."""

    name = "diagnose_math_work"

    description = (
        "Classify a student's math mistake after the student has provided "
        "both an answer and an explanation of their reasoning. Returns a "
        "probability for every predefined mistake category."
    )

    parameters_schema = {
        "type": "object",
        "properties": {
            "problem": {
                "type": "string",
                "description": "The complete math problem.",
            },
            "student_answer": {
                "type": "string",
                "description": "The student's final answer.",
            },
            "student_reasoning": {
                "type": "string",
                "description": (
                    "The student's explanation, intermediate steps, or "
                    "transcribed spoken reasoning."
                ),
            },
        },
        "required": [
            "problem",
            "student_answer",
            "student_reasoning",
        ],
    }

    async def __call__(
        self,
        deps: ToolDependencies,
        **kwargs: Any,
    ) -> dict[str, Any]:
        problem = str(kwargs.get("problem", "")).strip()
        student_answer = str(kwargs.get("student_answer", "")).strip()
        student_reasoning = str(
            kwargs.get("student_reasoning", "")
        ).strip()

        if not problem or not student_answer or not student_reasoning:
            return {
                "error": (
                    "problem, student_answer, and student_reasoning "
                    "must all be provided"
                )
            }

        try:
            # The Jev SDK is synchronous, so run it outside the app's
            # asynchronous conversation event loop.
            return await classify_with_jev(
                problem,
                student_answer,
                student_reasoning,
            )
        except Exception:
            logger.exception("Jev classification failed")
            return {"error": "The mistake classifier is currently unavailable."}