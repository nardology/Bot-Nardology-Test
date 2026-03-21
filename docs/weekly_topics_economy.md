# Weekly character topics (economy note)

- **Points:** +50 per matched topic (up to 3 per ISO week), same anti-cheat gates as the daily topic bonus (minimum message length / word count, keyword overlap, trivial repetition rejected).
- **Eligibility:** Sum of **today’s** daily quest progress (all daily quests, global wallet) must exceed **5**, and the user’s **selected** character (`active_style_id`) must match the `/talk` character for lazy generation.
- **Monday batch job:** Pre-generates rows using **yesterday’s** daily quest totals so the UTC morning bucket is not empty.
- **Storage:** One row per `(guild_id, user_id, style_id, week_id)`; production uses `guild_id = 0` (global) alongside the global points wallet.

Tuning payout or match strictness affects engagement and point inflation similarly to daily topic bonuses.
