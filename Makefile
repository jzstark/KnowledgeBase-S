.PHONY: dev dev-d build build-dev deploy down backup logs ps

dev:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile workers up

dev-d:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile workers up -d

build:
	docker compose build

build-dev:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile workers build

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
