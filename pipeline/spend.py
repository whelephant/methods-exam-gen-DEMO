"""Token/cost estimation and append-only spend logging.

Pricing is hard-coded for the three models we use. Numbers are USD per million tokens.
Cache-read pricing is the standard Anthropic 10% multiplier on input price.
Cache-write pricing is the standard 1.25x on input price.
Update MODEL_PRICING if Anthropic changes their rates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pipeline.db import connect


@dataclass(frozen=True)
class ModelPricing:
    name: str
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_per_mtok: float


MODEL_PRICING: dict[str, ModelPricing] = {
    "claude-haiku-4-5":  ModelPricing("claude-haiku-4-5",  0.80,  4.00, 0.08,  1.00),
    "claude-sonnet-4-6": ModelPricing("claude-sonnet-4-6", 3.00, 15.00, 0.30,  3.75),
    "claude-opus-4-7":   ModelPricing("claude-opus-4-7", 15.00, 75.00, 1.50, 18.75),
}

# Anthropic vision: image input ≈ ceil(width * height / 750) tokens.
# A 200 DPI A4 page is ~1700x2400 px → ~5500 tokens. We use a conservative 2000 default
# for budgeting purposes; actual usage comes back in the API response and we log that.
DEFAULT_PAGE_IMAGE_TOKENS = 2_000


def estimate_image_tokens(width_px: int, height_px: int) -> int:
    return max(1, (width_px * height_px + 749) // 750)


def estimate_cost(
    model: str,
    *,
    input_tokens: int = 0,
    cached_read_tokens: int = 0,
    cached_write_tokens: int = 0,
    output_tokens: int = 0,
) -> float:
    p = MODEL_PRICING[model]
    return (
        input_tokens * p.input_per_mtok
        + cached_read_tokens * p.cache_read_per_mtok
        + cached_write_tokens * p.cache_write_per_mtok
        + output_tokens * p.output_per_mtok
    ) / 1_000_000


def log_call(
    *,
    call_type: str,
    model: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_ms: Optional[int] = None,
    source_id: Optional[int] = None,
    page: Optional[int] = None,
    question_id: Optional[str] = None,
    prompt_hash: Optional[str] = None,
    ok: bool = True,
    error_message: Optional[str] = None,
) -> int:
    """Insert a row into extraction_log. Returns the new row id."""
    conn = connect()
    try:
        cur = conn.execute(
            """
            insert into extraction_log
              (call_type, source_id, page, question_id, model, prompt_hash,
               input_tokens, cached_tokens, output_tokens, cost_usd, latency_ms,
               ok, error_message)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (call_type, source_id, page, question_id, model, prompt_hash,
             input_tokens, cached_tokens, output_tokens, cost_usd, latency_ms,
             1 if ok else 0, error_message),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def total_spend(source_id: Optional[int] = None) -> float:
    conn = connect()
    try:
        if source_id is None:
            r = conn.execute("select coalesce(sum(cost_usd), 0.0) from extraction_log").fetchone()
        else:
            r = conn.execute(
                "select coalesce(sum(cost_usd), 0.0) from extraction_log where source_id = ?",
                (source_id,),
            ).fetchone()
        return float(r[0])
    finally:
        conn.close()


class BudgetExceeded(RuntimeError):
    pass


def check_budget(budget_usd: float, baseline_usd: float = 0.0) -> None:
    """Raise BudgetExceeded if spend since baseline exceeds the cap. Call before each API call."""
    spent = total_spend() - baseline_usd
    if spent >= budget_usd:
        raise BudgetExceeded(f"cumulative spend ${spent:.4f} >= budget ${budget_usd:.2f}")


if __name__ == "__main__":
    import json
    print(json.dumps({
        "models": list(MODEL_PRICING.keys()),
        "default_page_image_tokens": DEFAULT_PAGE_IMAGE_TOKENS,
        "example_cost_haiku_1page": estimate_cost(
            "claude-haiku-4-5",
            input_tokens=DEFAULT_PAGE_IMAGE_TOKENS,
            output_tokens=1200,
        ),
        "example_cost_sonnet_1page": estimate_cost(
            "claude-sonnet-4-6",
            input_tokens=DEFAULT_PAGE_IMAGE_TOKENS,
            output_tokens=1200,
        ),
        "total_spend_to_date": total_spend(),
    }, indent=2))
