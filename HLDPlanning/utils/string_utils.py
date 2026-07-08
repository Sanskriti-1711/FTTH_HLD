def normalize_key(value):
    """Normalize any ID or text to lowercase stripped string for safe comparisons."""
    if value is None:
        return ""
    return str(value).strip().lower()
