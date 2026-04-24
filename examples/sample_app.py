"""
examples/sample_app.py

Deliberately varied script to exercise Ghost:
- type-hint mismatches (anomaly detector will flag these)
- caught exceptions (exception rate tracking)
- recursion (call graph depth)
- a class with methods
- a slow function (latency outlier)
"""
import time


def add(a: int, b: int) -> int:
    return a + b


def greet(name: str) -> str:
    return f"hello, {name}"


def might_fail(x: int) -> int:
    if x < 0:
        raise ValueError(f"negative: {x}")
    return x * 2


def factorial(n: int) -> int:
    if n <= 1:
        return 1
    return n * factorial(n - 1)


def slow_operation(n: int) -> list:
    """Intentionally slow so latency outlier detector fires."""
    time.sleep(0.01)
    return list(range(n))


class DataProcessor:
    def __init__(self, label: str) -> None:
        self.label = label

    def process(self, items: list) -> dict:
        return {"label": self.label, "count": len(items), "sum": sum(items)}

    def reset(self) -> None:
        self.label = ""


if __name__ == "__main__":
    # Normal calls
    print(add(1, 2))
    print(add(10, 20))

    # Type mismatches — Ghost will flag these
    print(add(1.5, 2.5))       # float passed where int annotated
    print(greet("world"))
    print(greet(42))            # int passed where str annotated

    # Exception rate
    for v in [5, -1, 3, -2, 7, -3]:
        try:
            print(might_fail(v))
        except ValueError as e:
            print(f"caught: {e}")

    # Recursion
    print(factorial(8))

    # Latency outlier
    slow_operation(100)
    slow_operation(200)

    # Class
    dp = DataProcessor("test-run")
    print(dp.process([1, 2, 3, 10]))
    dp.reset()