from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from dateutil.rrule import (
    FREQNAMES,
    HOURLY,
    MINUTELY,
    SECONDLY,
    rrule,
    rruleset,
    rrulestr,
)
from devtools import pformat
from loguru import logger

from recurrent.event_parser import RE_STARTING, RecurringEvent

_TIME_ATTRS = ("_byhour", "_byminute", "_bysecond")
_TIME_FREQS = {HOURLY, MINUTELY, SECONDLY}


def _rule_has_time_components(rule: rrule) -> bool:
    # Any explicit BYHOUR/BYMINUTE/BYSECOND counts as time-bearing
    # for a in _TIME_ATTRS:
    #     v = getattr(rule, a, None)
    #     if v:  # non-empty list/tuple/int => time component present
    #         logger.debug(f"{a}={v}")
    #         return True
    # Also check that the freq itself is time-granular
    if getattr(rule, "_freq", None) in _TIME_FREQS:
        return True
    return False


def is_daily_or_greater(obj: rrule | rruleset) -> bool:
    """
    Return True if the parsed rrule/rruleset does not specify recurrence on the level of
    hour/minute/second.
    """
    # single rrule:
    if isinstance(obj, rrule):
        return not _rule_has_time_components(obj)

    # rruleset: it can contain multiple rrules
    if isinstance(obj, rruleset):
        # rruleset._rrule is a list of rrule objects
        for r in getattr(obj, "_rrule", ()):
            if _rule_has_time_components(r):
                return False
        # rruleset may have RDATEs with explicit datetimes; those are allowed
        # (they carry an explicit time — decide policy if you want to forbid)
        return True

    raise TypeError("Expected rrule or rruleset")


# TODO un-dataclass to be professional
@dataclass
class RecurrenceRule:
    input_str: str
    """either a natural language string, or a properly formatted rrule string"""
    start_dt: datetime
    """If timezone-awareness is desired, start_dt must be timezone-aware for local zone, 
    so everything downstream is also. Local zone is necessary here since recurrence will 
    be defined using local zone (can't expect user to consider the offset properly when 
    specifying specific weekdays etc.)"""
    num_preview: int = 5
    """number of preview datetimes to generate for `preview_dts`"""
    daily_or_greater_only: bool = True
    """If True, hourly, minutely, secondly recurrence not allowed. This is a sensible
    constraint for working with calendar APIs."""

    # computed members
    canonical: list[str] = field(init=False)
    """list of RRULE/EXRULE/RDATE/EXDATE strings required by some calendar APIs"""
    rr: rrule | rruleset = field(init=False)
    """the dateutil.rrule object holding detailed information"""
    input_form: Literal["rrule", "natural_language"] = field(init=False)
    """record of the form of input_str"""
    preview_dts: list[datetime] = field(init=False)
    """`num_preview` length list of preview datetimes generated"""

    def __post_init__(self):
        if "FREQ=" in self.input_str.upper():
            # user passed an already formatted RRULE, try parsing it directly
            rstr = self.input_str
            self.input_form = "rrule"
        else:
            # Use recurrent to parse natural language into an RFC rrule
            # NOTE: not currently using start_dt for the rule, but might as well pass it
            # since otherwise it uses now()
            r = RecurringEvent(now_date=self.start_dt, until_days_inclusive=False)

            if RE_STARTING.search(self.input_str):
                # user supplied explicit starting "baseline" phrase (could be in
                # reference to now like "starting next week")
                # TODO not working
                r.parse(self.input_str)
            else:
                r.parse(f"starting {self.start_dt}, " + self.input_str)

            if not r.is_recurring:
                raise ValueError(
                    f"{self.input_str=} could not be parsed as recurrence rule"
                )
            # get_RFC_rrule() yields an RFC str like "DTSTART:...\nRRULE:...",
            rstr = r.get_RFC_rrule()
            if r.dtstart and self.start_dt != r.dtstart:
                self.start_dt = r.dtstart
                logger.debug(f"RecurringEvent updated start_dt to {self.start_dt}")
            self.input_form = "natural_language"

        # for GcalEvent, have to split on '\n', and remove DTSTART and DTEND elements if present
        self.canonical = [
            s for s in rstr.splitlines() if "DTSTART" not in s and "DTEND" not in s
        ]
        logger.debug(f"{self.canonical=}")
        # Validate with dateutil, also handy to keep around full rrule object for
        # introspection
        self.rr = rrulestr("\n".join(self.canonical), dtstart=self.start_dt)

        logger.debug(f"{str(self.rr)=}")
        logger.debug(f"{pformat(rrule_to_dict(self.rr))}")

        if self.daily_or_greater_only and not is_daily_or_greater(self.rr):
            raise ValueError(
                "rrules must only specify recurring dates, not recurring times"
            )

        # produce preview datetimes
        self.preview_dts = list(self.rr[: self.num_preview])
        logger.debug(f"Preview datetimes: {pformat(self.preview_dts)}")

    def adjust_original_datetime(
        self, start: datetime, end: datetime
    ) -> tuple[datetime, datetime]:
        """
        Compare start date to the first recurrence preview datetime's date (both in
        UTC timezone). If they're not the same, there is an invalid inconsistency
        (which likely either came about from start using the default date of today, or
        the recurrence using a reference to start for its internal start like "next
        week"). In this case, the rule takes precedence, and we add the delta to both
        start and end and return the new datetimes.
        """
        if self.preview_dts[0].tzinfo is not None:
            first_date = self.preview_dts[0].astimezone(ZoneInfo("UTC")).date()
        else:
            first_date = self.preview_dts[0].date()

        start_date = start.date()
        if start_date != first_date:
            logger.info(
                f"{start_date=} and first recurring {first_date=} are not equal, "
                "we're assuming that the first recurring datetime is what's desired "
                "and adding the delta to `start` and `end`."
            )
            delta = first_date - start_date
            if delta < timedelta(0):
                raise AssertionError("negative timedelta shouldn't happen")
            start += delta
            end += delta
        return start, end


def rrule_to_dict(rule: rrule) -> dict:
    # pull out the useful fields; these attribute names are pragmatic & available
    d = {
        "freq": FREQNAMES[rule._freq],  # integer constant (DAILY/WEEKLY...)
        "interval": rule._interval,
        "count": rule._count,
        "until": rule._until.isoformat() if rule._until else None,
        "byweekday": rule._byweekday if rule._byweekday else None,
        "bymonthday": rule._bymonthday if rule._bymonthday else None,
        "bymonth": rule._bymonth if rule._bymonth else None,
        "byhour": rule._byhour if rule._byhour else None,
        "byminute": rule._byminute if rule._byminute else None,
        "bysecond": rule._bysecond if rule._bysecond else None,
        "dtstart": (
            rule._dtstart.isoformat() if getattr(rule, "_dtstart", None) else None
        ),
        # NOTE: bysetpos expands all the specified rules and then picks those with the
        # chosen index position
        "bysetpos": rule._bysetpos if rule._bysetpos else None,
    }
    # collapse empty lists to None for compact JSON
    return {k: v for k, v in d.items() if v is not None}


def rruleset_to_serializable(rrs: rruleset) -> dict:
    return {
        "rrules": [rrule_to_dict(r) for r in getattr(rrs, "_rrule", [])],
        "rdates": (
            [d.isoformat() for d in getattr(rrs, "_rdate", [])]
            if getattr(rrs, "_rdate", None)
            else []
        ),
        "exdates": (
            [d.isoformat() for d in getattr(rrs, "_exdate", [])]
            if getattr(rrs, "_exdate", None)
            else [],
        ),
    }


# def dict_to_rrule(d: dict) -> rrule:
#     kwargs = {}
#     if d.get("dtstart"):
#         kwargs["dtstart"] = datetime.fromisoformat(d["dtstart"])
#     if d.get("until"):
#         kwargs["until"] = datetime.fromisoformat(d["until"])
#     # map the rest
#     kwargs["freq"] = d["freq"]
#     if d.get("interval"):
#         kwargs["interval"] = d["interval"]
#     if d.get("count"):
#         kwargs["count"] = d["count"]
#     if d.get("byweekday"):
#         kwargs["byweekday"] = d["byweekday"]
#     if d.get("bymonthday"):
#         kwargs["bymonthday"] = d["bymonthday"]
#     if d.get("bymonth"):
#         kwargs["bymonth"] = d["bymonth"]
#     # ...etc
#     return rrule(**kwargs)


"""
>>> import datetime
>>> from recurrent.event_parser import RecurringEvent
>>> r = RecurringEvent(now_date=datetime.datetime(2010, 1, 1))
>>> r.parse('every day starting next tuesday until feb')
'DTSTART:20100105\nRRULE:FREQ=DAILY;INTERVAL=1;UNTIL=20100201'
>>> r.is_recurring
True
>>> r.get_params()
{'dtstart': '20100105', 'freq': 'daily', 'interval': 1, 'until': '20100201'}

>>> r.parse('feb 2nd')
datetime.datetime(2010, 2, 2, 0, 0)

>>> r.parse('not a date at all')

>>> r.format('DTSTART:20100105\nRRULE:FREQ=DAILY;INTERVAL=1;UNTIL=20100201')
'daily from Tue Jan 5, 2010 to Mon Feb 1, 2010'
>>> r.format(r.parse('fridays twice'))
'every Fri twice'
>>>
"""
