import os
import logging
from typing import Any

import httpx

from reachy_mini_conversation_app.transcript_log import append_transcript_lines
from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.90
MAX_CLARIFICATION_QUESTIONS = 5


MISTAKE_CATEGORIES = {
    "algebra_or_calculation_error": (
        "The differentiation method is appropriate, but the student made an algebraic simplification, "
        "sign, arithmetic, or other calculation error."
    ),
    "chain_rule_error": (
        "The student omitted, misapplied, or incorrectly completed the chain rule when differentiating a composite "
        "function."
    ),
    "reducing_exponent_error": (
        "The student incorrectly reduced, combined, rewrote, or simplified an exponent while differentiating."
    ),
    "product_or_quotient_rule_error": (
        "The student incorrectly selected, set up, or applied the product rule or quotient rule."
    ),
    "special_function_derivative_error": (
        "The student used an incorrect derivative for a trigonometric, exponential, or logarithmic expression."
    ),
    "other_error": (
        "The primary error does not fit any of the other categories, or the available work is insufficient to "
        "classify it reliably."
    ),
}

PROBLEMS = [
    "Differentiate f(x) = x^2.",
    "Differentiate f(x) = sin(x).",
]

CORRECT_ANSWERS = [
    "2x",
    "cos(x)",
]


def get_problem_and_answer(problem_number: int) -> tuple[str, str]:
    """Return the configured problem and answer for a one-based number."""
    if len(PROBLEMS) != len(CORRECT_ANSWERS):
        raise ValueError("PROBLEMS and CORRECT_ANSWERS must have the same length")
    if problem_number < 1 or problem_number > len(PROBLEMS):
        raise ValueError(f"problem_number must be between 1 and {len(PROBLEMS)}")

    index = problem_number - 1
    return PROBLEMS[index], CORRECT_ANSWERS[index]


def normalize_answer(answer: str) -> str:
    """Normalize superficial text differences before comparing answers."""
    normalized = answer.strip().lower().replace("−", "-").replace("^", "**")
    normalized = "".join(normalized.split())
    if normalized.startswith("y="):
        normalized = normalized[2:]
    return normalized


async def classify_with_jev(
    problem: str,
    correct_answer: str,
    student_answer: str,
    student_reasoning: str | None = None,
    clarification_history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Send the student's work directly to the Jev API."""
    api_key = os.getenv("TYPESAFE_API_KEY")
    if not api_key:
        raise RuntimeError("TYPESAFE_API_KEY is not configured")

    input_lines = [
        "",
        "=== JEV CLASSIFICATION INPUT ===",
        "Model: jev-latest",
        f"Problem: {problem}",
        f"Correct answer: {correct_answer}",
        f"Student answer: {student_answer}",
        f"Student reasoning: {student_reasoning or '(not provided)'}",
        f"Clarification history: {clarification_history or '(none)'}",
    ]
    for line in input_lines:
        print(line, flush=True)
    append_transcript_lines(input_lines)

    request_body = {
        "model": "jev-latest",
        "state": {
            "math_problem": problem,
            "correct_answer": correct_answer,
            "student_answer": student_answer,
            "student_reasoning": student_reasoning or "No reasoning was provided.",
            "clarification_history": clarification_history or [],
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
        category: round(float(probability), 4) for category, probability in answer["probabilities"].items()
    }

    ranked = sorted(
        probabilities.items(),
        key=lambda item: item[1],
        reverse=True,
    )

    top_probability = ranked[0][1]

    result_lines = ["=== JEV SORTED PERCENTAGES ==="]
    result_lines.extend(f"{category}: {probability * 100:.2f}%" for category, probability in ranked)
    result_lines.extend(
        [
            f"Selected category: {answer['choice']}",
            f"Confidence: {float(answer['confidence']) * 100:.2f}%",
            "",
        ]
    )
    for line in result_lines:
        print(line, flush=True)
    append_transcript_lines(result_lines)

    return {
        "category": answer["choice"],
        "confidence": float(answer["confidence"]),
        "probabilities": probabilities,
        "needs_clarification": top_probability < CONFIDENCE_THRESHOLD,
    }


class GetMathProblem(Tool):
    """Retrieve one configured problem by its one-based number."""

    name = "get_math_problem"
    description = (
        "Retrieve a configured math problem by number. Use this before starting each problem. "
        "The student already has a written copy, so say only 'Let's do problem N' unless they ask you to read it."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "problem_number": {
                "type": "integer",
                "minimum": 1,
                "description": "The one-based problem number to retrieve.",
            }
        },
        "required": ["problem_number"],
    }

    async def __call__(
        self,
        deps: ToolDependencies,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Return a configured problem without exposing its correct answer."""
        problem_number = kwargs.get("problem_number")
        if not isinstance(problem_number, int) or isinstance(problem_number, bool):
            return {"error": "problem_number must be an integer"}

        try:
            problem, _ = get_problem_and_answer(problem_number)
        except ValueError as error:
            return {"error": str(error)}

        return {
            "problem_number": problem_number,
            "total_problems": len(PROBLEMS),
            "problem": problem,
        }


class DiagnoseMathWork(Tool):
    """Classify the likely mistake in a student's math reasoning."""

    name = "diagnose_math_work"

    description = (
        "Check a student's answer to a configured math problem. If the answer matches the correct answer, return "
        "that it is correct without calling Jev. Otherwise, ask Jev to classify the error and return a probability "
        "for every predefined category. Student reasoning is optional. On repeated calls, include all previous "
        "clarification question-and-answer pairs in order. The result explicitly says whether to ask another "
        "question or advance."
    )

    parameters_schema = {
        "type": "object",
        "properties": {
            "problem_number": {
                "type": "integer",
                "minimum": 1,
                "description": "The one-based number of the problem the student is answering.",
            },
            "student_answer": {
                "type": "string",
                "description": "The student's final answer.",
            },
            "student_reasoning": {
                "type": "string",
                "description": (
                    "Optional explanation, intermediate steps, or transcribed spoken reasoning. Omit this when the "
                    "student gives only a final answer."
                ),
            },
            "clarification_history": {
                "type": "array",
                "maxItems": MAX_CLARIFICATION_QUESTIONS,
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "answer": {"type": "string"},
                    },
                    "required": ["question", "answer"],
                },
                "description": (
                    "Clarification question-and-answer pairs for this problem, in chronological order. Use an empty "
                    "list on the first classification and append one pair after each student response."
                ),
            },
        },
        "required": [
            "problem_number",
            "student_answer",
        ],
    }

    async def __call__(
        self,
        deps: ToolDependencies,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Validate the inputs and return Jev's mistake classification."""
        problem_number = kwargs.get("problem_number")
        student_answer = str(kwargs.get("student_answer", "")).strip()
        raw_reasoning = kwargs.get("student_reasoning")
        student_reasoning = str(raw_reasoning).strip() if raw_reasoning is not None else None
        raw_clarifications = kwargs.get("clarification_history", [])

        if not isinstance(problem_number, int) or isinstance(problem_number, bool):
            return {"error": "problem_number must be an integer"}
        if not student_answer:
            return {"error": "student_answer must be provided"}
        if not isinstance(raw_clarifications, list) or not all(
            isinstance(item, dict)
            and isinstance(item.get("question"), str)
            and item["question"].strip()
            and isinstance(item.get("answer"), str)
            and item["answer"].strip()
            for item in raw_clarifications
        ):
            return {"error": "clarification_history must contain non-empty question and answer strings"}
        if len(raw_clarifications) > MAX_CLARIFICATION_QUESTIONS:
            return {"error": f"at most {MAX_CLARIFICATION_QUESTIONS} clarification pairs are allowed"}

        clarification_history = [
            {"question": item["question"].strip(), "answer": item["answer"].strip()} for item in raw_clarifications
        ]
        clarification_count = len(clarification_history)

        try:
            problem, correct_answer = get_problem_and_answer(problem_number)
            if normalize_answer(student_answer) == normalize_answer(correct_answer):
                return {
                    "problem_number": problem_number,
                    "is_correct": True,
                    "category": None,
                    "probabilities": None,
                    "needs_clarification": False,
                    "clarification_questions_asked": clarification_count,
                    "clarification_questions_remaining": 0,
                    "next_action": "advance_to_next_problem",
                }

            result = await classify_with_jev(
                problem,
                correct_answer,
                student_answer,
                student_reasoning,
                clarification_history,
            )
            should_ask_another = result["needs_clarification"] and (clarification_count < MAX_CLARIFICATION_QUESTIONS)
            return {
                "problem_number": problem_number,
                "is_correct": False,
                "clarification_questions_asked": clarification_count,
                "clarification_questions_remaining": max(0, MAX_CLARIFICATION_QUESTIONS - clarification_count),
                "clarification_limit_reached": clarification_count >= MAX_CLARIFICATION_QUESTIONS,
                "next_action": ("ask_clarifying_question" if should_ask_another else "advance_to_next_problem"),
                **result,
            }
        except ValueError as error:
            return {"error": str(error)}
        except Exception:
            logger.exception("Jev classification failed")
            append_transcript_lines(
                [
                    "=== JEV CLASSIFICATION ERROR ===",
                    "The Jev request failed. See application.log for technical details.",
                    "",
                ]
            )
            return {"error": "The mistake classifier is currently unavailable."}
