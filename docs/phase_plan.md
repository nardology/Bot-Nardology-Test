# Product Phase Plan

Phased implementation of metrics, dashboards, community/UGC, and 5/10-pull shop features. Each phase is designed to be implemented independently to avoid overload.

---

## Phase 1: 5-Pull and 10-Pull Shop Items ✅ (Implemented)
**Goal:** Add bulk character purchases to the points shop with selection UI.

| Item | Description |
|------|-------------|
| Shop items | Add "Buy 5 Characters" (500 pts) and "Buy 10 Characters" (1000 pts) with 5️⃣ and 🔟 emojis |
| Multi-roll logic | Roll N unique characters (no duplicates within a pull); pity applies per roll |
| Opening animation | Box opening only (no spin); reuses existing assets |
| Selection UI | Multi-select checklist of which characters to add + Apply + Deny |
| Replace flow | When inventory overflows, second checklist to choose which owned characters to replace |

**Files:** `commands/slash/points.py`, `commands/slash/character.py`, `utils/character_store.py` (if batch helpers needed)

---

## Phase 2: Metrics & Dashboards – Foundation ✅ (Implemented)
**Goal:** Instrument key events and expose basic owner analytics.

| Item | Description |
|------|-------------|
| New event types | `trial_start`, `trial_end`, `conversion`, `pull_5`, `pull_10`, `churn_signal` |
| Analytics schema | Ensure `AnalyticsDailyMetric` / events support funnel columns |
| Owner analytics command | Extend `/owner` with basic funnel: new users → first roll → first talk → trial → paid |
| Conversion funnel | Simple aggregation: daily/weekly counts per stage |

**Files:** `utils/analytics.py`, `utils/audit.py`, `commands/slash/owner.py`, migrations if needed

---

## Phase 3: Metrics & Dashboards – Full ✅ (Implemented)
**Goal:** Production-ready dashboards and cost tracking.

| Item | Description |
|------|-------------|
| Revenue & LTV | MRR, ARPU per guild, trial→paid conversion rate |
| Retention | D1/D7/D30 retention (first roll / first talk as anchor) |
| Economy health | Points spent per user, shop item popularity (extra_roll, pull_5, pull_10, etc.) |
| AI cost & ROI | Tokens per call, cost per user, cost vs revenue by guild |
| Churn signals | Guilds with falling activity, trials ending without conversion |
| Webhook alerts | Optional: Discord webhook for new Pro conversions, anomalies |

**Files:** `utils/analytics.py`, `utils/metrics.py`, `commands/slash/owner.py`, optional Grafana configs

---

## Phase 4: Community & UGC – Discovery ✅ (Implemented)
**Goal:** Improve pack discovery and creator visibility.

| Item | Description |
|------|-------------|
| Pack marketplace / browse | Public or cross-server pack discovery beyond current `/packs browse` |
| Pack ratings / reviews | Optional: simple star rating or upvotes per pack |
| Creator leaderboard | Top pack creators by usage or ratings |
| Featured packs | Curated featured packs (admin-configurable) |

**Files:** `commands/slash/packs.py`, `utils/packs_store.py`, new models/Redis keys if needed

---

## Phase 5: Community & UGC – Monetization
**Goal:** Monetize UGC and incentivize creators.

| Item | Description |
|------|-------------|
| Creator revenue share | Optional: % of Pro revenue or points spent in packs |
| Pack subscriptions | Points or Pro-gated access to exclusive packs |
| Verification badge | Trusted creator badge for high-quality packs |
| Pro creator perks | "Unlimited packs" for Pro vs free cap |

**Files:** `utils/premium.py`, `utils/packs_store.py`, `core/entitlements.py`, Stripe integration when ready

---

## Phase 6: Stripe Integration (when ready)
**Goal:** Complete monetization with real payments.

| Item | Description |
|------|-------------|
| Stripe webhook | Handle subscription lifecycle |
| Entitlement sync | Map Stripe subscription → `PremiumEntitlement` |
| Billing portal | Optional: link for users to manage subscription |

**Files:** New `core/stripe_webhook.py`, `utils/premium.py`, `core/entitlements.py`

---

## Summary

| Phase | Focus | Dependencies |
|-------|-------|--------------|
| 1 | 5/10-pull shop | None |
| 2 | Metrics foundation | None |
| 3 | Full dashboards | Phase 2 |
| 4 | UGC discovery | None |
| 5 | UGC monetization | Phase 4 (optional) |
| 6 | Stripe | Phase 2 (for conversion tracking) |
