"""Report unavailable Windows symlink privilege without masking other failures."""
import os


def symlink_or_skip(test, *args, **kwargs):
    try:
        return os.symlink(*args, **kwargs)
    except OSError as error:
        if getattr(error, 'winerror', None) == 1314:
            test.skipTest('Windows symlink privilege unavailable')
        raise
