.PHONY: dev dev-d build build-dev deploy down down-dev backup logs ps refactor-smoke

dev:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile workers up --remove-orphans

dev-d:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile workers up -d --remove-orphans

build:
	docker compose build

build-dev:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile workers build

deploy:
	./deploy.sh

down:
	docker compose down

down-dev:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile workers down --remove-orphans

backup:
	./scripts/backup.sh

logs:
	docker compose logs -f

ps:
	docker compose ps

refactor-smoke:
	python scripts/refactor_smoke.py
