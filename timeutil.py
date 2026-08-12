"""Japan-time "today", independent of the server's system timezone.

This app is used by a Japan-based household but may run on infrastructure in
another timezone (e.g. UTC on Streamlit Community Cloud). Plain date.today()
follows the server's local clock, which would shift "today"/"this month"/
"this year" boundaries by up to 9 hours for JST users - use today_jst()
everywhere instead of date.today().
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def today_jst() -> date:
    return datetime.now(JST).date()
