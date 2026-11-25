"""Provision only the fixed teaching image; reuse existing launch audit policy."""
import secrets

from django.core.exceptions import ValidationError
from django.db import connection
from django.views.decorators.debug import sensitive_variables

from .checker_secrets import seal, unseal
from .models import CheckerEndpoint, TeamService
from .mutation import serialized_mutation

IMAGE = "ctf-training-notes:local"


@sensitive_variables()
@serialized_mutation(durable=True)
def _prepare(service):
    endpoint = CheckerEndpoint.objects.filter(team_service=service).first()
    if endpoint is None:
        endpoint = CheckerEndpoint(team_service=service)
        endpoint.token_ciphertext = seal(secrets.token_hex(32), f"endpoint:{endpoint.instance_id}")
        endpoint.save()
    return endpoint


@serialized_mutation(durable=True)
def _record(service, endpoint, container_id, port):
    current = CheckerEndpoint.objects.get(pk=endpoint.pk)
    if current.instance_id != endpoint.instance_id:
        raise ValidationError("Изменился экземпляр учебного сервиса.")
    current.container_id, current.port = container_id, port
    current.save(update_fields=["container_id", "port"])
    TeamService.objects.filter(pk=service.pk).update(container_id=container_id, host_port=port, status="running")


def _validated_port(container, endpoint):
    container.reload()
    labels = container.attrs.get("Config", {}).get("Labels", {}) or {}
    config = container.attrs.get("HostConfig", {})
    bindings = (container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}).get("5000/tcp") or []
    if (labels.get("ctf.checker_instance") != str(endpoint.instance_id)
            or labels.get("ctf.team_service_id") != str(endpoint.team_service_id)
            or len(bindings) != 1 or bindings[0].get("HostIp") != "127.0.0.1"
            or not config.get("ReadonlyRootfs") or config.get("Privileged")
            or container.attrs.get("Config", {}).get("User") != "10001:10001"):
        raise ValidationError("Параметры учебного контейнера не соответствуют ожидаемым.")
    port = int(bindings[0]["HostPort"])
    if not 1024 <= port <= 65535:
        raise ValidationError("Недопустимый порт учебного сервиса.")
    return port


@sensitive_variables()
def deploy_training_service(service):
    if connection.in_atomic_block:
        raise RuntimeError("Docker cannot run inside a database transaction")
    from .infrastructure import get_docker_client
    endpoint = _prepare(service)
    token = unseal(endpoint.token_ciphertext, f"endpoint:{endpoint.instance_id}")
    client = get_docker_client()
    try:
        if service.container_id:
            # Missing/replaced instances require launch review, never recreation.
            container = client.containers.get(service.container_id)
            port = _validated_port(container, endpoint)
            if endpoint.container_id != service.container_id or endpoint.port != port:
                raise ValidationError("Изменился адрес учебного контейнера.")
            if container.status == "exited":
                container.start()
            elif container.status != "running":
                raise ValidationError("Учебный контейнер не готов.")
        else:
            if endpoint.container_id:
                raise ValidationError("Нужно сверить сохранённый учебный контейнер.")
            container = client.containers.run(
                IMAGE, detach=True, ports={"5000/tcp": ("127.0.0.1", None)},
                environment={"CHECKER_TOKEN": token, "CHECKER_INSTANCE": str(endpoint.instance_id)},
                labels={"ctf.team_service_id": str(service.pk), "ctf.checker_instance": str(endpoint.instance_id)},
                user="10001:10001", read_only=True, cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"], mem_limit="128m", nano_cpus=500_000_000, pids_limit=64,
                tmpfs={"/tmp": "rw,noexec,nosuid,size=8m", "/data": "rw,noexec,nosuid,size=16m,uid=10001,gid=10001,mode=0700"},
            )
            port = _validated_port(container, endpoint)
        # Preserve ID in memory too, for the existing launch compensation path.
        service.container_id, service.host_port, service.status = container.id, port, "running"
        _record(service, endpoint, container.id, port)
        return container
    finally:
        client.close()
