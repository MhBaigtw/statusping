from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from rest_framework.exceptions import ValidationError

from .models import User, normalize_email

EMAIL_TAKEN = "An account with this email already exists."


def register_user(*, email, password):
    email = normalize_email(email)
    try:
        validate_password(password, user=User(email=email))
    except DjangoValidationError as exc:
        raise ValidationError({"password": list(exc.messages)}) from exc
    if User.objects.filter(email=email).exists():
        raise ValidationError({"email": [EMAIL_TAKEN]})
    try:
        with transaction.atomic():
            return User.objects.create_user(email, password)
    except IntegrityError as exc:  # lost a race with a concurrent registration
        raise ValidationError({"email": [EMAIL_TAKEN]}) from exc
