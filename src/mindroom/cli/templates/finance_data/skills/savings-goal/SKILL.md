---
name: savings-goal
description: Work out how much to save each month to reach a goal by a date
---

# Savings goal

Information, not advice.
This skill shows the arithmetic for one goal; the person chooses the plan.

## Collect

1. The goal amount, the target date, and what is already saved toward it.
2. The monthly amount they could put aside.
3. The interest rate the savings would earn, as an assumption with its source and date; use 0 when they do not know.

## Work out

Compute every step with `calculator` and show the inputs, with `n` the number of months until the target date and `r` the monthly rate.
For a yearly rate quoted as APY or AER, which already includes compounding, `r` is (1 + yearly rate)^(1/12) − 1; for a nominal yearly rate, `r` is the yearly rate ÷ 12; ask which one it is when unsure.

- **Without interest,** or when `r` is 0: the monthly amount needed is (goal − saved) ÷ n, and their amount reaches saved + monthly × n.
- **With interest:** the monthly amount needed is (goal − saved × (1 + r)ⁿ) × r ÷ ((1 + r)ⁿ − 1).
- **Their amount, with interest:** what they would reach by the date is saved × (1 + r)ⁿ + monthly × ((1 + r)ⁿ − 1) ÷ r.

## Report

Lead with the monthly amount needed and how it compares with what they can put aside.
When there is a gap, show what closes it: a smaller goal or a larger monthly amount, each with its number, or a later date.
For a later date, (goal − saved) ÷ monthly gives the months needed without interest, an upper bound when there is interest; check a shorter `n` with the formula for their amount.
List the assumptions, such as a fixed rate and no withdrawals.

Offer to save the goal and plan under **Goals** in `MEMORY.md`, and save it only if the person agrees.
