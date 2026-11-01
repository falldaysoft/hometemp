from django import template
from django.utils import timezone

register = template.Library()


@register.filter
def ago(ts):
    """Compact relative time: 'just now', '4 min ago', '2 h ago', 'Mon 9:14 am'."""
    if not ts:
        return "never"
    s = (timezone.now() - ts).total_seconds()
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return timezone.localtime(ts).strftime("%a %-I:%M %p").replace("AM", "am").replace("PM", "pm")


@register.filter
def every(seconds):
    if seconds is None:
        return "—"
    if seconds < 90:
        return f"every {seconds:.0f} s"
    if seconds < 5400:
        return f"every {seconds / 60:.0f} min"
    return f"every {seconds / 3600:.1f} h"


def _num(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


@register.filter
def temp(v):
    v = _num(v)
    return "—" if v is None else f"{v:.1f}°"


@register.filter
def signed(v):
    v = _num(v)
    return "" if v is None else f"{'+' if v > 0 else '−' if v < 0 else '±'}{abs(v):.1f}°"


@register.filter
def get_item(d, key):
    return d.get(key) if d else None


@register.filter
def invert(v):
    v = _num(v)
    return None if not v else 1 / v


@register.filter
def mul(v, n):
    v = _num(v)
    return None if v is None else v * float(n)
