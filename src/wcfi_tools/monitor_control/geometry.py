"""Physical-pixel geometry shared by window capture and input gating."""

Rect = tuple[int, int, int, int]


def intersection(first: Rect, second: Rect) -> Rect | None:
    x, y = max(first[0], second[0]), max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    return (x, y, right - x, bottom - y) if right > x and bottom > y else None


def contains(rect: Rect, point: tuple[int, int]) -> bool:
    return (rect[0] <= point[0] < rect[0] + rect[2]
            and rect[1] <= point[1] < rect[1] + rect[3])


def overlap_area(first: Rect, second: Rect) -> int:
    overlap = intersection(first, second)
    return overlap[2] * overlap[3] if overlap is not None else 0
