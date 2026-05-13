# Decision-quality judge rubric

You are evaluating whether trip-tracker's alert-decision model behaved
correctly for one fixture. The model produced `{"alert": bool, "reason": str}`.
The fixture carries `expected_alert: bool` and `expected_reason_themes:
list[str]`.

Grade the model's output on two axes:

1. **Alert correctness** — does `actual.alert == expected_alert`?
2. **Reason quality** — does the model's `reason` string touch at least one
   of the `expected_reason_themes`, in spirit? Themes are short phrases the
   fixture author hand-labelled; the model's exact wording will differ. If
   the expected list is empty, treat reason quality as automatically
   satisfied (no themes to match).

Output strict JSON, nothing else:

    {"verdict": "pass" | "fail", "rationale": "<one short sentence>"}

`pass` requires BOTH axes correct. Any other state — alert mismatch,
reason that fails to engage with any theme, missing/malformed model
output — is `fail`. Keep the rationale under 200 characters. No prose
outside the JSON, no markdown fences, no extra keys.
