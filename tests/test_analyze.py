"""The README check in tools/benchmark/analyze.py."""
from tools.benchmark.analyze import readme_mismatches

README = """# Title

## Cloud performance and determinism: test

| Condition | Runs |
|---|---:|
| **Warm** | 59 |

CloudWatch recorded 60 plan invocations. More context follows here.

**Cost:** $0.0455 for the 60 runs.

## Run it yourself

| Not | Checked |
"""

GENERATED = [
    "| Condition | Runs |",
    "| **Warm** | 59 |",
    "CloudWatch recorded 60 plan invocations.",
    "**Cost:** $0.0455 for the 60 runs.",
]


def test_matching_readme_passes_and_later_sections_are_ignored():
    assert readme_mismatches(README, GENERATED) == (4, [])


def test_a_changed_figure_is_caught():
    changed = README.replace("| 59 |", "| 58 |")
    checked, missing = readme_mismatches(changed, GENERATED)
    assert missing == ["| **Warm** | 58 |"]


def test_a_sentence_that_no_longer_starts_with_the_figures_is_caught():
    changed = README.replace("$0.0455", "$0.05")
    assert readme_mismatches(changed, GENERATED)[1] == [
        "**Cost:** $0.05 for the 60 runs."]
