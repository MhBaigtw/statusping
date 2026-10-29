from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"

    def ready(self):
        from . import checks  # noqa: F401  (registers the system checks)
