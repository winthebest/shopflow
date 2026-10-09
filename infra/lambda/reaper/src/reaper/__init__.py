"""Tear down an expired shopflow EKS session through AWS APIs only.

Used twice with the same code: as the backup Lambda kill switch (``reaper.handler``) and as a CLI
(``python -m reaper``) from the GitHub reaper workflow and ``cloud-down --force-api``.
"""
