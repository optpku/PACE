"""Project guidance defaults for exact model and reasoning-effort pairs."""

MODEL_RESOURCE_DEFAULTS: dict[tuple[str, str], str] = {
    ("gpt-5.5", "medium"): "standard",
    ("gpt-5.5", "high"): "standard",
    ("gpt-5.5", "xhigh"): "standard",
    ("gpt-5.6-sol", "high"): "standard",
    ("gpt-5.6-sol", "xhigh"): "standard",
    ("gpt-6.1-sol", "medium"): "standard",
    ("gpt-6.1-sol", "high"): "standard",
    ("gpt-6.1-sol", "xhigh"): "standard",
    ("deepseek-v4-flash", "none"): "weak",
}


def default_resource_variant(model: str, reasoning_effort: str) -> str:
    """Use detailed guidance for pairs not explicitly listed by the project."""

    return MODEL_RESOURCE_DEFAULTS.get((model, reasoning_effort), "weak")
