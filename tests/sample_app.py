"""
examples/sample_app.py

A deliberately varied little script to exercise the observer:
- regular functions with different arg types
- a class with methods
- an exception that gets caught
- recursion
- a type mismatch that Ghost will later flag as an anomaly
"""


def add(a: int, b: int) -> int:
    return a + b


def greet(name: str) -> str:
    return f"hello, {name}"


def might_fail(x):
    if x < 0:
        raise ValueError(f"negative: {x}")
    return x * 2


def factorial(n: int) -> int:
    if n <= 1:
        return 1
    return n * factorial(n - 1)


class DataProcessor:
    def __init__(self, label: str):
        self.label = label

    def process(self, items: list) -> dict:
        return {"label": self.label, "count": len(items), "sum": sum(items)}

    def reset(self):
        self.label = ""


# ── main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(add(1, 2))
    print(add(10, 20))
    # Type mismatch: hint says int but we pass a float — Ghost will catch this
    print(add(1.5, 2.5))

    print(greet("world"))
    print(greet(42))        # another type mismatch: str expected, int passed

    for v in [5, -1, 3]:
        try:
            print(might_fail(v))
        except ValueError as e:
            print(f"caught: {e}")

    print(factorial(8))

    dp = DataProcessor("test-run")
    print(dp.process([1, 2, 3, 10]))
    dp.reset()
