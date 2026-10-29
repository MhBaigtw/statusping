"""One error shape for every API error:

{"error": {"code": "<machine_code>", "message": "<human message>", "details": {...}}}
"""

import math

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import exceptions, status
from rest_framework.views import exception_handler as drf_exception_handler


class Conflict(exceptions.APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The request conflicts with the current state of the resource."
    default_code = "conflict"

    def __init__(self, detail=None, code=None, details=None):
        super().__init__(detail, code)
        self.details = details or {}


def error_body(code, message, details=None):
    return {"error": {"code": code, "message": message, "details": details or {}}}


def exception_handler(exc, context):
    if isinstance(exc, Http404):
        exc = exceptions.NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = exceptions.PermissionDenied()

    response = drf_exception_handler(exc, context)
    if response is None:
        return None  # unhandled -> Django's 500 handler

    if isinstance(exc, exceptions.ValidationError):
        details = (
            response.data
            if isinstance(response.data, dict)
            else {"non_field_errors": response.data}
        )
        response.data = error_body("validation_error", "Invalid input.", details)
        return response

    detail = exc.detail
    if isinstance(detail, dict):
        # SimpleJWT token errors: {"detail": ..., "code": ..., "messages": [...]}
        message = str(detail.get("detail", exc.default_detail))
        code = str(detail.get("code", exc.default_code))
        details = {k: v for k, v in detail.items() if k not in ("detail", "code")}
    else:
        message = str(detail)
        code = getattr(detail, "code", None) or exc.default_code
        details = getattr(exc, "details", {})
        if isinstance(exc, exceptions.Throttled) and exc.wait is not None:
            # DRF also sets the Retry-After header on this response.
            details = {"retry_after_seconds": math.ceil(exc.wait)}
    response.data = error_body(code, message, details)
    return response
