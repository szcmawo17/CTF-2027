import docker

from .models import TeamService


def get_docker_client():
    return docker.from_env()


def deploy_team_service(team_service):
    client = get_docker_client()

    # Проверяем, есть ли уже контейнер
    if team_service.container_id:
        try:
            container = client.containers.get(
                team_service.container_id
            )

            if container.status == "running":
                print("Контейнер уже работает:", container.id)
                return container

            if container.status == "exited":
                container.start()
                container.reload()

                team_service.status = "running"
                team_service.save(update_fields=["status"])

                print("Существующий контейнер запущен:", container.id)
                return container

            raise RuntimeError(
                f"Контейнер находится в состоянии: {container.status}"
            )

        except docker.errors.NotFound:
            print("Старый контейнер не найден. Создаём новый.")

    # Создаём контейнер
    container = client.containers.run(
        "ctf-test-service",
        detach=True,
        ports={"5000/tcp": None},
        labels={
            "ctf.team_service_id": str(team_service.id)
        }
    )

    container.reload()

    port = container.attrs["NetworkSettings"]["Ports"][
        "5000/tcp"
    ][0]["HostPort"]

    # Сохраняем информацию в базе
    team_service.container_id = container.id
    team_service.host_port = int(port)
    team_service.status = "running"

    team_service.save(
        update_fields=[
            "container_id",
            "host_port",
            "status"
        ]
    )

    print(
        f"Развёрнут сервис {team_service.service} "
        f"для команды {team_service.team}: порт {port}"
    )

    return container

def stop_team_service(team_service):
    """
    Останавливает контейнер сервиса команды.
    """

    if not team_service.container_id:
        raise ValueError("У сервиса отсутствует контейнер.")

    client = get_docker_client()

    container = client.containers.get(
        team_service.container_id
    )

    if container.status == "running":
        container.stop(timeout=10)

    team_service.status = "stopped"
    team_service.save(update_fields=["status"])

    return container


def restart_team_service(team_service):
    """
    Запускает ранее остановленный контейнер.
    """

    if not team_service.container_id:
        raise ValueError("У сервиса отсутствует контейнер.")

    client = get_docker_client()

    container = client.containers.get(
        team_service.container_id
    )

    if container.status != "running":
        container.start()

    container.reload()

    team_service.status = "running"
    team_service.save(update_fields=["status"])

    return container