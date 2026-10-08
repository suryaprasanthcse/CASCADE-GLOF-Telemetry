"""Ground-truth constants locked by the team on 8 Oct 2026 (Day 1).

Sources are listed in docs/architecture-brief.md, section 4.2. These are
validation benchmarks and hindcast inputs; the lake measurements never
read them.
"""
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# South Lhonak Lake surface area before the flood, reported by ISRO/NRSC.
PRE_FLOOD_AREA_HA = 167.4

# Flow distance from the lake down to Chungthang.
LAKE_TO_CHUNGTHANG_KM = 67.5

# Water the outburst released.
DRAINED_VOLUME_M3 = 50e6

# Flood arrival at Chungthang, when the Teesta-III dam was breached. The
# lake's own release time is still unknown; validation test R3 needs it.
TEESTA_III_BREACH_TIME = datetime(2023, 10, 4, 0, 30, tzinfo=IST)
