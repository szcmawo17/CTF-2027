import docker

client = docker.from_env()

container = client.containers.run(
    "hello-world",
    detach=True,
    name="ctf-test"
)

print("Контейнер создан!")
print("ID:", container.short_id)
print("Статус:", container.status)