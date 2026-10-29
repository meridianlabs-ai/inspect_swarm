import math
from collections.abc import Sequence
from typing import cast

# Checks of a channel factory's arguments, whose errors name the factory.


def check_bound(factory: str, name: str, value: object, low: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < low:
        raise ValueError(
            f"{factory}(): {name} must be a whole number of at least {low:,}, got {value!r}."
        )


def check_rate(factory: str, rate: object) -> tuple[int, float]:
    # a tuple, or the list a log gives back
    if isinstance(rate, tuple | list) and len(cast(Sequence[object], rate)) == 2:
        count, window = cast(Sequence[object], rate)
        if (
            isinstance(count, int)
            and not isinstance(count, bool)
            and count >= 1
            and isinstance(window, int | float)
            and not isinstance(window, bool)
            and 0 < window < math.inf
        ):
            return (count, float(window))
    raise ValueError(
        f"{factory}(): rate must be (count, seconds), a positive whole count and a finite, positive window in seconds, or None; got {rate!r}."
    )
