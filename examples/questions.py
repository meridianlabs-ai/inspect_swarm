"""Questions with exact answers, shared by the examples that need no sandbox."""

from inspect_ai.dataset import Sample

QUESTIONS = [
    Sample(
        id="prime",
        input="What is the smallest prime number greater than 1000? Answer with the number only.",
        target="1009",
    ),
    Sample(
        id="zeros",
        input="How many trailing zeros does 100! (100 factorial) have? Answer with the number only.",
        target="24",
    ),
    Sample(
        id="digits",
        input="What is the sum of the decimal digits of 2 to the power 15? Answer with the number only.",
        target="26",
    ),
]
