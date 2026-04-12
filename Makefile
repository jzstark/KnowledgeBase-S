.PHONY: dev build deploy down backup logs ps

dev:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml up

dev-d:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

build:
	docker compose build

deploy:
	./deploy.sh

down:
	docker compose down

backup:
	./scripts/backup.sh

logs:
	docker compose logs -f

ps:
	docker compose ps
