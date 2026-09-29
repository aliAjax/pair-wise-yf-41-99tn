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


# 台站修正报文中，会触发撤回发布稿、生成待复核修订的实质性字段。
MATERIAL_FIELDS = ("location", "latitude", "longitude", "origin_time", "magnitude")

# 可接收台站修正报文的事件状态（候选/已关联事件由关联环节处理）。
CORRECTABLE_STATUSES = (
    "associated",
    "reviewed",
    "published",
    "revised",
    "revision_pending",
    "withdrawn",
)


def is_material_change(current, updated, fields=MATERIAL_FIELDS):
    """震中、发震时刻或震级发生变化即视为实质性修订。"""
    for field in fields:
        before = current.get(field)
        after = updated.get(field, before)
        if after is not None and str(after) != str(before):
            return True
    return False


def merge_correction(current, correction):
    """按台站编号归并修正报文；仅补波形时保留既有审校结论。

    返回 (合并后的事件 data, 本台站报文, 是否新增波形段)。
    """
    if not correction.get("station"):
        raise ValidationError("correction requires a station code")
    merged = dict(current)
    reports = {
        item.get("station"): dict(item)
        for item in (current.get("reports") or [])
        if item.get("station")
    }
    report = dict(correction)
    station = report.pop("station")
    existing = reports.get(station) or {"station": station}

    waveforms = list(existing.get("waveforms") or [])
    added = 0
    for waveform in report.pop("waveforms", None) or []:
        channel = waveform.get("channel")
        if channel and any(item.get("channel") == channel for item in waveforms):
            continue  # 同通道重复补传，按幂等处理
        waveforms.append(waveform)
        added += 1
    if waveforms:
        existing["waveforms"] = waveforms

    for key, value in report.items():
        if value is not None:
            existing[key] = value
    reports[station] = dict(existing)
    merged["reports"] = list(reports.values())

    for field in MATERIAL_FIELDS:
        value = correction.get(field)
        if value is not None:
            merged[field] = value
    return merged, reports[station], bool(added)


CUSTOM_CREATE = {'station': _validate_station, 'event': _validate_event}
CUSTOM_TRANSITIONS = {('event', 'associate'): _validate_associate}


class RuleEngine:
    ALIASES = {'stations': 'station', 'events': 'event'}
    INITIAL_STATUS = {'station': 'online', 'event': 'candidate'}
    TRANSITIONS = {'station': {'offline': (('online',), 'offline'), 'online': (('offline',), 'online')}, 'event': {'associate': (('candidate',), 'associated'), 'review': (('associated', 'revision_pending'), 'reviewed'), 'publish': (('reviewed',), 'published'), 'revise': (('published', 'revised', 'revision_pending'), 'revised'), 'withdraw': (('published', 'revised', 'revision_pending'), 'withdrawn')}}
    CREATE_REQUIRED = {'station': ('code', 'lat', 'lon'), 'event': ('title', 'origin_time', 'location', 'reports')}
    ACTION_REQUIRED = {('station', 'offline'): ('reason',), ('event', 'review'): ('reviewer', 'magnitude'), ('event', 'publish'): ('communication_id',), ('event', 'revise'): ('reason', 'magnitude'), ('event', 'withdraw'): ('reason',)}
    CREATE_ROLES = {'station': ('admin', 'station'), 'event': ('admin', 'analyst')}
    ROLE_ACTIONS = {'offline': ('admin', 'station'), 'online': ('admin', 'station'), 'associate': ('admin', 'analyst'), 'review': ('admin', 'reviewer'), 'publish': ('admin', 'reviewer'), 'revise': ('admin', 'reviewer'), 'withdraw': ('admin', 'reviewer'), 'submit_correction': ('admin', 'station', 'analyst')}

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

    def plan_correction(self, actor, entity, correction):
        """计算台站修正报文落地后的事件数据、状态与修订记录内容。

        - 补波形（非实质性）：保留原状态与审校结论。
        - 震中/发震时刻/震级变化（实质性）：已发布事件撤回发布稿，
          未发布事件同样进入待复核，均生成 pending_review 修订。
        """
        kind = self.normalize_kind(entity["kind"])
        if kind != "event":
            raise InvalidTransition("corrections apply to events only")
        if entity["status"] not in CORRECTABLE_STATUSES:
            raise InvalidTransition(
                "cannot correct event from status %s" % entity["status"]
            )
        self._ensure_role(
            actor, self.ROLE_ACTIONS.get("submit_correction", ("admin",))
        )
        if not correction.get("station"):
            raise ValidationError("correction requires a station code")

        current = entity["data"]
        merged, station_report, added_waveform = merge_correction(
            current, dict(correction)
        )
        material = is_material_change(current, merged)

        if material:
            to_status = "revision_pending"
            revision_status = "pending_review"
        else:
            # 仅补波形：审校结论保留，发布稿不撤回
            to_status = entity["status"]
            revision_status = "applied"

        changed_fields = [
            field
            for field in MATERIAL_FIELDS
            if str(current.get(field)) != str(merged.get(field))
        ]
        if added_waveform and "waveforms" not in changed_fields:
            changed_fields.append("waveforms")

        revision = {
            "station": correction["station"],
            "message_id": correction.get("message_id"),
            "material": material,
            "revision_status": revision_status,
            "changed_fields": changed_fields,
            "station_report": station_report,
            "reason": correction.get("reason"),
        }
        return to_status, merged, revision


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
