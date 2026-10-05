"""Japan price bounds expressed through Qlib's standard Exchange configuration."""


def execution_limit_expressions(deal_price: str) -> tuple[str, str]:
    price = str(deal_price).removeprefix("$")
    if price not in {"open", "close", "vwap"}:
        raise ValueError(f"Unsupported JP daily execution price: {price}")
    # Bounds and quotes share Qlib's adjustment factor. Float32 feature storage
    # needs a small relative tolerance at the exact price boundary.
    return (
        f"((${price} >= $jp_limit_up * (1 - 1e-7)) | $jp_unavailable)",
        f"((${price} <= $jp_limit_down * (1 + 1e-7)) | $jp_unavailable)",
    )
