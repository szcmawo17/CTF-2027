"""Trusted local worker. Claim/execute/record can later use another transport."""
from django.core.exceptions import ValidationError
from django.db import connection
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .checker_http import HTTPResult, bounded_http
from .checker_jobs import claim_job, record_result
from .checker_secrets import unseal
from .models import CheckerJob, FlagMaterial


@sensitive_variables()
def execute_claim(claim):
    if connection.in_atomic_block:
        raise RuntimeError("HTTP checker cannot run inside a database transaction")
    job = CheckerJob.objects.select_related("endpoint", "flag__round", "flag__team_service").get(pk=claim.job_id)
    if job.state != "running" or job.lease != claim.lease or job.lease_until <= timezone.now():
        return HTTPResult("ERROR", "expired")
    endpoint, service = job.endpoint, job.flag.team_service
    if (endpoint.team_service_id != service.pk or endpoint.instance_id != job.instance_id
            or endpoint.port != job.port or service.host_port != job.port
            or endpoint.container_id != job.container_id or service.container_id != job.container_id):
        return HTTPResult("ERROR", "endpoint")
    try:
        token = unseal(endpoint.token_ciphertext, f"endpoint:{endpoint.instance_id}")
        payload = {"port": job.port, "instance": str(job.instance_id), "token": token,
                   "expires_at": job.flag.round.expires_at.timestamp(), "operation": job.operation,
                   "flag": job.flag_id, "team_service": service.pk, "digest": job.flag.digest}
        if job.operation == "put_flag":
            material = FlagMaterial.objects.get(flag=job.flag, expires_at__gt=timezone.now())
            payload["value"] = unseal(material.ciphertext, f"flag:{job.flag_id}")
    except (ValidationError, FlagMaterial.DoesNotExist):
        return HTTPResult("ERROR", "secret")
    return bounded_http(payload)


def run_one():
    if connection.in_atomic_block:
        raise RuntimeError("Worker must run outside a database transaction")
    claim = claim_job()
    if claim is None:
        return False
    result = execute_claim(claim)
    record_result(claim, result)
    return True
