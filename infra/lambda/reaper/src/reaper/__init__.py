"""Tear down an expired shopflow EKS session through AWS APIs only.

Used twice with the same code: as the Lambda kill switch (``reaper.handler``, run by the hourly sweep and by the
per-session schedule) and as a CLI (``python -m reaper``) from ``cloud-down --force-api``.
"""
