def clean_title_text(value: str) -> str | None:
    title = " ".join(value.split())
    return title or None
