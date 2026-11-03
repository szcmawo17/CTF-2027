from contextlib import contextmanager
from functools import wraps

from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import F

from .models import CompetitionMutationLock


@contextmanager
def mutation_transaction(*, durable=False):
    """First SQL statement acquires a write lock, before reading roster state.

    SQLite serializes database writers; PostgreSQL locks this singleton row.
    Deliberately coarse for the local prototype. Never hold this over Docker.
    Busy requests fail safely and can be retried by the caller.
    """
    try:
        with transaction.atomic(durable=durable):
            if not CompetitionMutationLock.objects.filter(pk=1).update(revision=F("revision") + 1):
                raise ValidationError("Не создана блокировка состава. Примените миграции.")
            yield
    except IntegrityError as error:
        raise ValidationError("Операция конфликтует с существующим участием или ролью.") from error
    except OperationalError as error:
        if "locked" not in str(error).lower() and "busy" not in str(error).lower():
            raise
        raise ValidationError("Данные сейчас изменяются другим запросом. Повторите действие.") from error


def serialized_mutation(function=None, *, durable=False):
    if function is None:
        return lambda wrapped: serialized_mutation(wrapped, durable=durable)
    @wraps(function)
    def wrapped(*args, **kwargs):
        with mutation_transaction(durable=durable):
            return function(*args, **kwargs)
    return wrapped
