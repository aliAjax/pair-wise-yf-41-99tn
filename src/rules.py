from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_station(actor, data, lookup):
    if not data.get("code"):
        raise ValidationError("station code is required")


def _validate_event(actor, data, lookup):
    reports = data.get("reports") or []
    if len(reports) < 2:
        raise ValidationError("event requires at least two station reports")
    if not data.get("title"):
        raise ValidationError("event title is required")


def _validate_associate(actor, entity, data, lookup):
    reports = entity["data"].get("reports") or []
    if len(reports) < 2:
        raise ValidationError("two reports are required for association")
    return {"associated_count": len(reports)}


def associate_reports(reports, max_delta=120, max_distance=3.0):
    if not reports:
        return []
    anchor = reports[0]
    result = [anchor]
    for report in reports[1:]:
        if abs(float(report.get("time_offset", 0))) <= max_delta and float(report.get("distance_km", 0)) <= max_distance:
            result.append(report)
    return result


def magnitude_median(amplitudes):
    values = sorted(float(value) for value in amplitudes)
    if not values:
        raise ValidationError("amplitudes are required")
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


CUSTOM_CREATE = {'station': _validate_station, 'event': _validate_event}
CUSTOM_TRANSITIONS = {('event', 'associate'): _validate_associate}


class RuleEngine:
    ALIASES = {'stations': 'station', 'events': 'event'}
    INITIAL_STATUS = {'station': 'online', 'event': 'candidate'}
    TRANSITIONS = {
        'station': {
            'offline': (('online',), 'offline'),
            'online': (('offline',), 'online'),
        },
        'event': {
            'associate': (('candidate',), 'associated'),
            'review': (('associated', 'pending_review'), 'reviewed'),
            'publish': (('reviewed',), 'published'),
            'revise': (('published', 'revised'), 'revised'),
            'withdraw': (('published', 'revised'), 'withdrawn'),
        },
    }
    # 台站修正报文可在初审、发布、修订等各阶段陆续到达
    CORRECTION_ALLOWED_STATUS = (
        'candidate', 'associated', 'reviewed',
        'published', 'revised', 'pending_review',
    )
    CORRECTION_RELEASED_STATUS = ('published', 'revised')
    CORRECTION_ROLES = ('admin', 'station')
    SIGNIFICANT_FIELDS = ('origin_time', 'location', 'magnitude')
    CREATE_REQUIRED = {'station': ('code', 'lat', 'lon'), 'event': ('title', 'origin_time', 'location', 'reports')}
    ACTION_REQUIRED = {('station', 'offline'): ('reason',), ('event', 'review'): ('reviewer', 'magnitude'), ('event', 'publish'): ('communication_id',), ('event', 'revise'): ('reason', 'magnitude'), ('event', 'withdraw'): ('reason',)}
    CREATE_ROLES = {'station': ('admin', 'station'), 'event': ('admin', 'analyst')}
    ROLE_ACTIONS = {'offline': ('admin', 'station'), 'online': ('admin', 'station'), 'associate': ('admin', 'analyst'), 'review': ('admin', 'reviewer'), 'publish': ('admin', 'reviewer'), 'revise': ('admin', 'reviewer'), 'withdraw': ('admin', 'reviewer')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch

    def plan_correction(self, actor, entity, data, lookup=None):
        """根据台站修正报文计算新数据、目标状态和变化类型。

        - 按台站编号归并：同站报告覆盖（波形保留），新站报告追加；
        - 仅补波形：审校结论保留，状态不变；
        - 震中/发震时刻/震级变化：撤回发布稿，进入待复核。
        """
        kind = self.normalize_kind(entity["kind"])
        if kind != "event":
            raise InvalidTransition("corrections apply to events only")
        self._ensure_role(actor, self.CORRECTION_ROLES)
        if entity["status"] not in self.CORRECTION_ALLOWED_STATUS:
            raise InvalidTransition(
                "cannot correct event from status %s" % entity["status"]
            )
        payload = dict(data or {})
        station_code = payload.get("station")
        if not station_code:
            raise ValidationError("correction requires a station code")
        known = None
        if lookup is not None:
            known = _find_one(lookup, "station", "code", station_code)
        if known is None and lookup is not None:
            raise ValidationError("unknown station code: " + str(station_code))

        current = dict(entity["data"])
        merged_reports = _merge_station_report(current.get("reports") or [], payload)
        if len(merged_reports) < 2:
            raise ValidationError("two reports are required after correction")
        merged = dict(current)
        merged["reports"] = merged_reports

        significant_patch = {}
        for field in self.SIGNIFICANT_FIELDS:
            if field in payload:
                value = payload[field]
                if field == "magnitude":
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        raise ValidationError("magnitude must be numeric")
                if str(current.get(field)) != str(value):
                    significant_patch[field] = value
                    merged[field] = value

        waveform = payload.get("waveform")
        supplement = waveform not in (None, "", [], {})
        if not significant_patch and not supplement:
            raise ValidationError(
                "correction must carry waveform or a significant field"
            )

        significant = bool(significant_patch)
        if significant and entity["status"] in self.CORRECTION_RELEASED_STATUS:
            withdrawn = list(current.get("withdrawn_releases") or [])
            if current.get("communication_id"):
                withdrawn.append(
                    {
                        "communication_id": current.get("communication_id"),
                        "reason": payload.get("reason") or "significant correction",
                    }
                )
            merged["withdrawn_releases"] = withdrawn
            next_status = "pending_review"
        else:
            next_status = entity["status"]
        change = "significant" if significant else "waveform_supplement"
        return {
            "data": merged,
            "status": next_status,
            "change": change,
            "station": station_code,
            "significant": significant,
            "significant_patch": significant_patch,
        }


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


# 修正报文中属于台站报告（而非事件级震中/震级）的字段
_REPORT_FIELDS = ("time_offset", "distance_km", "waveform")


def _merge_station_report(reports, correction):
    """按台站编号归并修正报文：同站覆盖并保留已有波形，新站追加。"""
    station_code = correction["station"]
    incoming = {"station": station_code}
    for field in _REPORT_FIELDS:
        if correction.get(field) is not None:
            incoming[field] = correction[field]
    merged = [dict(report) for report in reports]
    for report in merged:
        if report.get("station") == station_code:
            for field, value in incoming.items():
                if value not in (None, "", [], {}):
                    report[field] = value
            break
    else:
        merged.append(incoming)
    return merged


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
