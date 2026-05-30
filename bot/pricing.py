_TIERS: list[tuple[int | float, int]] = [
    (100 * 2**20,       75),   # ≤ 100 MB
    (500 * 2**20,      150),   # ≤ 500 MB
    (2   * 2**30,      300),   # ≤ 2 GB
    (5   * 2**30,      600),   # ≤ 5 GB
    (float("inf"),    1000),   # 5 GB+
]


def stars_for_size(size_bytes: int | None, *, is_audio: bool = False) -> int:
    if is_audio:
        return 25
    if size_bytes is None:
        return 150
    for threshold, stars in _TIERS:
        if size_bytes <= threshold:
            return stars
    return 1000


def fmt_bytes(n: int) -> str:
    if n >= 2**30:
        return f"{n / 2**30:.1f} GB"
    if n >= 2**20:
        return f"{n / 2**20:.0f} MB"
    return f"{n / 2**10:.0f} KB"
