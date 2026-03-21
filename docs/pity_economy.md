# Pity tuning (rarity odds)

Soft pity ramps live in `utils/character_registry.choose_rarity`. Hard caps: legendary at 99 consecutive misses, mythic at 999 (see `utils/pity_display.py`).

Environment variables (optional, default `1.0` / defaults below):

| Variable | Default | Effect |
|----------|---------|--------|
| `PITY_LEGENDARY_RAMP_MULT` | `1.0` | Scales the **+0.05% per miss** legendary soft ramp after 20 misses. |
| `PITY_LEGENDARY_MAX_P` | `0.10` | Max legendary roll probability before rare/uncommon checks (cap). |
| `PITY_MYTHIC_RAMP_MULT` | `1.0` | Scales the **+0.002% per miss** mythic soft ramp after 200 misses. |
| `PITY_MYTHIC_MAX_P` | `0.01` | Max mythic probability in the mythic branch. |

Raising ramps or caps increases **legendary/mythic** frequency, which affects **duplicate shard** income and overall economy.
