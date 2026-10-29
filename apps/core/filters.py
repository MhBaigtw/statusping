import django_filters


class StrictBooleanFilter(django_filters.TypedChoiceFilter):
    """Accepts only `true` or `false`; anything else is a 400 (a typo is not silently ignored)."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("choices", [("true", "true"), ("false", "false")])
        kwargs.setdefault("coerce", lambda value: value == "true")
        super().__init__(*args, **kwargs)
