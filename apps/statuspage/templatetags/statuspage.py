from django import template

register = template.Library()


@register.filter
def uptime(value):
    """98.5 -> "98.50%"; None -> "no data"."""
    return "no data" if value is None else f"{value:.2f}%"


@register.filter
def duration(seconds):
    """Seconds -> "45 s", "12 min", "3 h 5 min"; None -> "ongoing"."""
    if seconds is None:
        return "ongoing"
    if seconds < 60:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


@register.filter
def get(mapping, key):
    return mapping.get(key)
