from django.conf import settings


def local_trusted_mode():
    return bool(getattr(settings, 'LOCAL_TRUSTED_MODE', False))
